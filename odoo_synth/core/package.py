"""Package the masked result into a restorable artifact.

Primary artifact: a pg_dump custom-format file of the masked scratch DB
(`<out>/db.dump`). This is what `odoo-synth up` restores.

manifest.json records: schema hash (pg_dump --schema-only hash), per-table
row counts, the rulebook version/hash applied, and a timestamp -- enough
for a consumer to verify what they're restoring and for `rules diff` to
detect rulebook drift.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import pgtools
from .rulebook import Rulebook
from .schema import snapshot_from_db


class PackageError(Exception):
    """Raised when packaging fails."""


@dataclass
class PackageConfig:
    db_url: str  # masked scratch DB URL
    out: Path
    # rulebook hash for the manifest (caller computes; we just record it).
    rulebook_dir: Path | None = None


def package(cfg: PackageConfig, rulebook: Rulebook | None = None) -> Path:
    """Produce the output artifact from a masked scratch DB.

    Writes:
      <out>/db.dump             # pg_dump -Fc custom format (primary)
      <out>/manifest.json       # schema hash, row counts, rulebook hash, ts

    Returns <out>. Raises PackageError on any failure.
    """
    out = Path(cfg.out)
    out.mkdir(parents=True, exist_ok=True)

    # 1. Primary: pg_dump custom format.
    dump_path = out / "db.dump"
    _pg_dump_custom(cfg.db_url, dump_path)

    # 2. Schema hash (from a schema-only plain dump, sha256).
    schema_hash = _schema_hash(cfg.db_url)

    # 2b. Schema snapshot sidecar -- the oracle `rules scan`/`rules diff`
    # compare the rulebook against. Built from the catalogs (lossy but
    # enough for the PII-shape classifier). Written to schema.json next to
    # the manifest so a bundle is self-describing.
    try:
        snap = snapshot_from_db(cfg.db_url)
        (out / "schema.json").write_text(snap.to_json(), "utf-8")
        schema_source = snap.source
        schema_tables = len(snap.tables)
    except Exception as exc:
        # A missing snapshot makes `rules scan` refuse (it needs the oracle),
        # but packaging the primary artifact should still succeed. Record
        # the failure in the manifest so it's not silently absent.
        schema_source = f"error: {exc}"
        schema_tables = 0

    # 3. Row counts per table.
    row_counts = _row_counts(cfg.db_url)

    # 4. manifest.json
    manifest: dict[str, Any] = {
        "source": "odoo_synth_snapshot",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "schema_hash": schema_hash,
        "row_counts": row_counts,
        "rulebook_hash": _rulebook_hash(cfg.rulebook_dir) if cfg.rulebook_dir else None,
        "rulebook_strategies": len(rulebook.strategies) if rulebook else None,
        "rulebook_field_rules": len(rulebook.field_rules) if rulebook else None,
        "primary_artifact": "db.dump",
        "schema_source": schema_source,
        "schema_tables": schema_tables,
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), "utf-8"
    )
    return out


# ---------------------------------------------------------------------------
# pg_dump
# ---------------------------------------------------------------------------



def _dump_db_url(db_url: str) -> str:
    """DB URL for pg_dump specifically (the masked scratch DB package()
    dumps). Honors an explicit ODOO_SYNTH_PACKAGE_DB_URL override for the
    in-container socket form; pgtools.run_pg_tool's automatic fallback
    rewrites the URL itself when no explicit override is set, so this is
    only consulted for the initial (host) attempt."""
    import os
    return os.environ.get("ODOO_SYNTH_PACKAGE_DB_URL", db_url)


def _pg_dump_custom(db_url: str, out_file: Path) -> None:
    # Pipe to stdout (not -f <path>) so it works through `docker exec`
    # redirects where -f would be container-relative. pgtools handles the
    # host/major-version-mismatch -> scratch-container fallback
    # automatically (see core/pgtools.py); no ODOO_SYNTH_PG_DUMP needed for
    # the common case of the bundled docker-compose.scratch.yml stack.
    with open(out_file, "wb") as fh:
        try:
            pgtools.run_pg_tool(
                "pg_dump", ["-Fc"], _dump_db_url(db_url),
                cmd_env="ODOO_SYNTH_PG_DUMP",
                url_envs=("ODOO_SYNTH_PACKAGE_DB_URL",),
                output_file=fh,
            )
        except pgtools.PgToolError as exc:
            raise PackageError(str(exc)) from exc


def _schema_hash(db_url: str) -> str:
    """sha256 of a schema-only plain-text dump -- a stable identity of the
    masked DB's structure, independent of row data."""
    try:
        proc = pgtools.run_pg_tool(
            "pg_dump", ["--schema-only", "--no-owner", "--no-privileges"],
            _dump_db_url(db_url),
            cmd_env="ODOO_SYNTH_PG_DUMP",
            url_envs=("ODOO_SYNTH_PACKAGE_DB_URL",),
        )
    except pgtools.PgToolError as exc:
        raise PackageError(f"pg_dump --schema-only failed:\n{exc}") from exc
    return hashlib.sha256(proc.stdout.encode("utf-8")).hexdigest()


def _row_counts(db_url: str) -> dict[str, int]:
    """Count rows per table in the public schema."""
    import psycopg
    counts: dict[str, int] = {}
    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT relname FROM pg_class c JOIN pg_namespace n "
                "ON c.relnamespace=n.oid WHERE n.nspname='public' "
                "AND c.relkind='r' ORDER BY relname"
            )
            tables = [r[0] for r in cur.fetchall()]
            for tbl in tables:
                cur.execute(f'SELECT count(*) FROM "{tbl}"')
                counts[tbl] = cur.fetchone()[0]
    return counts


def _rulebook_hash(rules_dir: Path) -> str:
    """sha256 over the concatenated rulebook files (sorted by name)."""
    h = hashlib.sha256()
    for f in sorted(rules_dir.glob("*.yml")):
        h.update(f.read_bytes())
        h.update(b"\x00")
    return h.hexdigest()
