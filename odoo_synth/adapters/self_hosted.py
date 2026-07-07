"""Self-hosted Odoo adapter: odoo-bin db dump/load if available, else
pg_dump -Fd -j N + filestore rsync.

Two extraction paths, one bundle shape:

  * odoo-bin path (preferred when odoo-bin is on PATH): `odoo-bin db dump
    <dbname>` produces a zip with the DB dump + filestore together. We
    unpack it into the standard bundle layout so downstream code doesn't
    care which path produced it.
  * pg_dump path (fallback): `pg_dump -Fd -j <N> <dbname>` for the DB
    plus an rsync/tar of the filestore directory. Same bundle layout.

Bundle layout (both paths):
    <out>/
      dump/          # pg_dump -Fd directory OR a dump.sql (odoo-bin path)
      dump.sql       # present for odoo-bin path (the plain SQL from the zip)
      filestore/     # filestore blobs (or empty if no filestore / policy drop)
      manifest.json  # {source: 'self_hosted'|'odoo_sh', odoo_version, ...}

Provisioning side: load() restores a bundle into a fresh DB via odoo-bin
(where available) or pg_restore, used by core/provision.py.

The attachment policy from rules/50_attachments.yml applies at the
filestore level too: if the default policy drops attachment content, we
don't bother copying filestore blobs for `full_scrub_filename_for_models`
-- no point moving bytes we're about to discard. (DB-row-level content
masking still happens in core/mask.py; this is the on-disk half.)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any



class SelfHostedError(Exception):
    """Raised when self-hosted extraction/provisioning fails."""


# ---------------------------------------------------------------------------
# odoo-bin detection
# ---------------------------------------------------------------------------


def find_odoo_bin(explicit: str | None = None) -> str | None:
    """Return a usable odoo-bin path, or None.

    Checks an explicit path first, then PATH. Verifies the path is
    executable. We do NOT run it here -- just confirm it exists.
    """
    if explicit:
        p = Path(explicit)
        if p.exists() and os.access(p, os.X_OK):
            return str(p)
        # maybe it's on PATH even though it looks like a name
        found = shutil.which(explicit)
        return found
    return shutil.which("odoo-bin") or shutil.which("odoo")


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    """Run a command, raising SelfHostedError on failure with the full cmd."""
    try:
        return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw)
    except FileNotFoundError as exc:
        raise SelfHostedError(
            f"required tool not found: {cmd[0]}. Install it or set an explicit path."
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise SelfHostedError(
            f"command failed (exit {exc.returncode}): {' '.join(cmd)}\n"
            f"stdout: {exc.stdout}\nstderr: {exc.stderr}"
        ) from exc


# ---------------------------------------------------------------------------
# Extraction (dump)
# ---------------------------------------------------------------------------


@dataclass
class DumpConfig:
    db_url: str  # psycopg URL for the source DB
    db_name: str
    filestore_dir: str | None = None  # Odoo filestore path (for pg_dump path)
    odoo_bin: str | None = None
    pg_jobs: int = 4
    # If True and the attachment default policy drops content, skip copying
    # filestore blobs for the full-scrub models (no point moving bytes we'll
    # discard). Set by the caller from rules/50_attachments.yml.
    drop_attachment_content: bool = True
    full_scrub_models: list[str] | None = None


def dump(cfg: DumpConfig, out: Path) -> Path:
    """Dump a self-hosted Odoo DB + filestore into <out>/ (bundle layout).

    Picks the odoo-bin path if odoo-bin is available, else the pg_dump path.
    Writes manifest.json describing the bundle.
    """
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "filestore").mkdir(exist_ok=True)

    odoo_bin = find_odoo_bin(cfg.odoo_bin)
    if odoo_bin:
        source = "self_hosted_odoo_bin"
        _dump_via_odoo_bin(odoo_bin, cfg, out)
    else:
        source = "self_hosted_pg_dump"
        _dump_via_pg_dump(cfg, out)

    _write_manifest(out, {
        "source": source,
        "odoo_version": _detect_odoo_version(cfg),
        "db_name": cfg.db_name,
        "filestore_copied": any((out / "filestore").iterdir()),
    })
    return out


def _dump_via_odoo_bin(odoo_bin: str, cfg: DumpConfig, out: Path) -> None:
    """odoo-bin db dump produces a zip; unpack into the bundle layout."""
    zip_path = out / "source_dump.zip"
    _run([odoo_bin, "db", "dump", cfg.db_name, "--format=zip"],
         stdout=open(zip_path, "wb"))
    # The zip contains dump.sql + dump.db (optional) + filestore/.
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(out / "dump_unpacked")
    # Normalize: move dump.sql to bundle root, filestore to bundle/filestore.
    unpacked = out / "dump_unpacked"
    sql = unpacked / "dump.sql"
    if sql.exists():
        shutil.copy2(sql, out / "dump.sql")
    fs = unpacked / "filestore"
    if fs.exists() and fs.is_dir():
        _copy_filestore_filtered(fs, out / "filestore", cfg)


def _dump_via_pg_dump(cfg: DumpConfig, out: Path) -> None:
    """pg_dump -Fd directory format + filestore copy.

    Uses the full connection URL (not the bare db name) so it works against
    remote/containerized Postgres. The ODOO_SYNTH_PG_DUMP env override (see
    core/package.py) is honored so a host with an older pg_dump can route
    through the container.
    """
    # Custom-format dump to a single file. We pipe to stdout (rather than
    # -f <path>) so it works through `docker exec` redirects where the -f path
    # would be container-relative. The file lands on the host FS.
    dump_file = out / "db.dump"
    pg_dump = _pg_dump_binary()
    proc = subprocess.run(
        pg_dump + ["-Fc", _dump_db_url(cfg.db_url)],
        stdout=open(dump_file, "wb"), stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        raise SelfHostedError(
            f"pg_dump failed (exit {proc.returncode}): "
            f"{proc.stderr.decode('utf-8', 'replace')}"
        )
    if cfg.filestore_dir:
        src = Path(cfg.filestore_dir)
        if src.exists() and src.is_dir():
            _copy_filestore_filtered(src, out / "filestore", cfg)


def _copy_filestore_filtered(src: Path, dst: Path, cfg: DumpConfig) -> None:
    """Copy filestore, skipping blobs for full-scrub models when policy drops content.

    The filestore layout is <filestore>/<db_name>/<sha1-of-content>/<files>.
    We can't cheaply map a blob back to its ir_attachment.res_model from the
    filesystem alone (that's in the DB), so when the default policy drops
    attachment content we simply skip copying ALL filestore blobs -- the DB
    pass nulls ir_attachment.datas/store_fname anyway, so the files are dead
    weight. This is the conservative reading of 50_attachments.yml: if content
    is dropped (the default), no filestore bytes are carried at all.
    """
    if cfg.drop_attachment_content:
        # Leave dst empty -- no filestore bytes shipped. Documented in the
        # manifest so a consumer knows filestore/ was intentionally empty.
        return
    # content kept -> copy everything.
    if src.exists() and src.is_dir():
        for item in src.iterdir():
            target = dst / item.name
            if item.is_dir():
                shutil.copytree(item, target, dirs_exist_ok=True)
            else:
                shutil.copy2(item, target)


# ---------------------------------------------------------------------------
# Provisioning (load) -- used by core/provision.py
# ---------------------------------------------------------------------------


def load(artifact: Path, db_name: str, db_url: str, odoo_bin: str | None = None) -> None:
    """Restore a dumped bundle into a fresh database named db_name.

    odoo-bin path: `odoo-bin db load --neutralize <dbname> <zip>`.
    pg_dump path: `pg_restore -d <dbname> <dump_dir>` (neutralize is the
    caller's job -- provision.py runs odoo-bin --neutralize separately when
    odoo-bin is available; when it isn't, the caller must neutralize another
    way, e.g. setting the relevant ir.config_parameter keys).
    """
    artifact = Path(artifact)
    odoo_bin = find_odoo_bin(odoo_bin)
    if odoo_bin and (artifact / "source_dump.zip").exists():
        _run([odoo_bin, "db", "load", "--neutralize", db_name,
              str(artifact / "source_dump.zip")])
        return
    # pg_dump directory restore.
    dump_dir = artifact / "dump"
    if not dump_dir.exists():
        # maybe a plain dump.sql
        sql = artifact / "dump.sql"
        if sql.exists():
            _run(["psql", "-d", db_url, "-f", str(sql)])
            return
        raise SelfHostedError(
            f"bundle has no restorable dump: expected {dump_dir} or "
            f"{artifact/'dump.sql'}"
        )
    _run(["pg_restore", "-d", db_url, "--no-owner", "--no-privileges",
          "-j", "4", str(dump_dir)])


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------





def _dump_db_url(cfg_db_url: str) -> str:
    """DB URL for pg_dump specifically. Honors ODOO_SYNTH_DUMP_DB_URL so the
    dump subprocess (which may run inside a container via ODOO_SYNTH_PG_DUMP)
    can connect to the in-container socket while psycopg connects from host."""
    import os
    return os.environ.get("ODOO_SYNTH_DUMP_DB_URL", cfg_db_url)


def _pg_dump_binary() -> list[str]:
    """Resolve the pg_dump command, honoring ODOO_SYNTH_PG_DUMP (see
    core/package.py). Shared so the adapter and the packager use the same
    override."""
    import os
    import shlex
    override = os.environ.get("ODOO_SYNTH_PG_DUMP")
    if override:
        return shlex.split(override)
    return ["pg_dump"]


def _write_manifest(out: Path, fields: dict[str, Any]) -> None:
    mf = out / "manifest.json"
    # Merge with any existing manifest (odoo_sh ingest may have written one).
    existing = {}
    if mf.exists():
        try:
            existing = json.loads(mf.read_text("utf-8"))
        except Exception:
            existing = {}
    existing.update(fields)
    mf.write_text(json.dumps(existing, indent=2, sort_keys=True), "utf-8")


def _detect_odoo_version(cfg: DumpConfig) -> str | None:
    """Best-effort: query ir_module_module for the 'base' module's version.

    Returns None if the source DB isn't reachable or has no base module.
    """
    try:
        import psycopg
        with psycopg.connect(cfg.db_url, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT latest_version FROM ir_module_module "
                    "WHERE name='base' LIMIT 1"
                )
                row = cur.fetchone()
                return row[0] if row else None
    except Exception:
        return None
