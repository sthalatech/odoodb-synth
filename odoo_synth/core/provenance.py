"""Detect the source Odoo deployment's shape, so a replica can reproduce it.

odoo-synth's data pipeline (dump -> mask -> restore) reproduces the *data*
layer of a production Odoo. A faithful running replica also needs the layers
*around* the data: the Odoo core version, the installed module set and the
addon code that backs it, the PostgreSQL major version, and the Python/runtime
the source ran under. This module detects those and records them as a single
``provenance.json`` sidecar in the bundle.

Detection is layered by what's reachable, and each layer degrades gracefully
so a DB-only snapshot still produces a useful (if partial) report rather than
failing:

  * **DB-derived** (always available from ``--source-db-url``): PostgreSQL
    ``server_version``, the Odoo series/base version (``ir_module_module``
    row for ``base``), the full installed-module list with versions, and the
    database encoding. This layer never needs filesystem or shell access.

  * **Filesystem-derived** (when ``--odoo-conf`` and/or an ``addons_path`` is
    provided): the resolved ``addons_path`` directories, and for each addon
    directory on disk its manifest ``version``, a content hash, and the git
    commit of its containing repo when present. This is what lets the replica
    step *verify* the target already carries matching addon code (decision A3:
    verify, don't bundle).

  * **Runtime-derived** (when ``--odoo-bin`` is provided): ``odoo-bin
    --version`` and the Python interpreter version behind that odoo-bin. Only
    the source runtime can report its own Python; when odoo-bin isn't given we
    record the fact as unknown rather than guessing from the tool's own Python.

The central reconciliation: every module marked *installed* in the database
MUST have its code present on the target for Odoo to boot. ``detect()`` cross-
references the installed-module list against the addon code discovered on disk
and records any installed module whose code was NOT found (``missing_addons``).
The replica preflight turns that list into a hard failure on the target.

Nothing here masks or mutates anything -- it is read-only detection.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


class ProvenanceError(Exception):
    """Raised only for hard failures (e.g. the source DB is unreachable).

    Missing *optional* inputs (no odoo.conf, no odoo-bin) are NOT errors --
    they downgrade the corresponding layer to ``unknown`` and are surfaced in
    the report's ``warnings`` list instead.
    """


# ---------------------------------------------------------------------------
# Report shape
# ---------------------------------------------------------------------------


@dataclass
class ModuleInfo:
    """One row from the source's ``ir_module_module``, installed state only."""

    name: str
    installed_version: str  # ir_module_module.latest_version
    # Path where the addon's code was found on disk (None = not found, which
    # for an installed module is a replica blocker).
    code_path: str | None = None
    # Manifest 'version' declared in __manifest__.py on disk, if found.
    manifest_version: str | None = None


@dataclass
class AddonRepo:
    """A directory on ``addons_path`` and its version-control provenance."""

    path: str
    git_remote: str | None = None
    git_commit: str | None = None
    is_dirty: bool | None = None  # uncommitted changes present?
    addon_count: int = 0  # number of addon dirs directly under this path


@dataclass
class ProvenanceReport:
    """Everything a replica needs to reproduce the source's non-data layers."""

    # --- DB-derived (always present) ---
    source_db_name: str = ""
    postgres_version: str = ""  # e.g. "16.14"
    postgres_major: int = 0  # e.g. 16
    db_encoding: str = ""
    odoo_series: str = ""  # e.g. "19.0"
    odoo_base_version: str = ""  # e.g. "19.0.1.3"
    installed_module_count: int = 0
    installed_modules: list[ModuleInfo] = field(default_factory=list)

    # --- Filesystem-derived (optional) ---
    addons_path: list[str] = field(default_factory=list)
    addon_repos: list[AddonRepo] = field(default_factory=list)
    # Installed modules whose code was NOT found on any addons_path dir --
    # a hard blocker for the replica (Odoo won't boot without the code).
    missing_addons: list[str] = field(default_factory=list)

    # --- Runtime-derived (optional) ---
    odoo_bin_version: str | None = None
    python_version: str | None = None

    # --- Meta ---
    detected_at: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_db_name": self.source_db_name,
            "postgres_version": self.postgres_version,
            "postgres_major": self.postgres_major,
            "db_encoding": self.db_encoding,
            "odoo_series": self.odoo_series,
            "odoo_base_version": self.odoo_base_version,
            "installed_module_count": self.installed_module_count,
            "installed_modules": [asdict(m) for m in self.installed_modules],
            "addons_path": list(self.addons_path),
            "addon_repos": [asdict(r) for r in self.addon_repos],
            "missing_addons": sorted(self.missing_addons),
            "odoo_bin_version": self.odoo_bin_version,
            "python_version": self.python_version,
            "detected_at": self.detected_at,
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ProvenanceReport":
        rep = cls(
            source_db_name=d.get("source_db_name", ""),
            postgres_version=d.get("postgres_version", ""),
            postgres_major=d.get("postgres_major", 0),
            db_encoding=d.get("db_encoding", ""),
            odoo_series=d.get("odoo_series", ""),
            odoo_base_version=d.get("odoo_base_version", ""),
            installed_module_count=d.get("installed_module_count", 0),
            addons_path=list(d.get("addons_path", [])),
            missing_addons=list(d.get("missing_addons", [])),
            odoo_bin_version=d.get("odoo_bin_version"),
            python_version=d.get("python_version"),
            detected_at=d.get("detected_at", ""),
            warnings=list(d.get("warnings", [])),
        )
        rep.installed_modules = [
            ModuleInfo(**m) for m in d.get("installed_modules", [])
        ]
        rep.addon_repos = [AddonRepo(**r) for r in d.get("addon_repos", [])]
        return rep


# ---------------------------------------------------------------------------
# Detection entrypoint
# ---------------------------------------------------------------------------


def detect(
    source_db_url: str,
    *,
    odoo_conf: str | Path | None = None,
    addons_path: list[str] | None = None,
    odoo_bin: str | None = None,
) -> ProvenanceReport:
    """Detect source provenance from a live source DB plus optional hints.

    ``source_db_url`` is required and read-only. ``odoo_conf`` (path to the
    source ``odoo.conf``) and/or an explicit ``addons_path`` enable the
    filesystem layer; ``odoo_bin`` enables the runtime layer. Missing optional
    hints downgrade their layer to ``unknown`` and add a warning, never raise.
    """
    import time

    report = ProvenanceReport(detected_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))

    # --- Layer 1: DB-derived (required) ---
    _detect_from_db(source_db_url, report)

    # --- Layer 2: filesystem-derived (optional) ---
    resolved_paths = _resolve_addons_path(odoo_conf, addons_path, report)
    if resolved_paths:
        report.addons_path = resolved_paths
        _detect_addons(resolved_paths, report)
    else:
        report.warnings.append(
            "no addons_path (pass --odoo-conf or --addons-path): cannot verify "
            "installed-module code presence; missing_addons will be empty and "
            "the replica preflight can't guarantee the target has matching code."
        )

    # --- Layer 3: runtime-derived (optional) ---
    if odoo_bin:
        _detect_runtime(odoo_bin, report)
    else:
        report.warnings.append(
            "no --odoo-bin: odoo_bin_version and source python_version left "
            "unknown (only the source runtime can report its own Python)."
        )

    return report


# ---------------------------------------------------------------------------
# Layer 1: database
# ---------------------------------------------------------------------------


def _detect_from_db(source_db_url: str, report: ProvenanceReport) -> None:
    import psycopg

    try:
        conn = psycopg.connect(source_db_url, autocommit=True)
    except psycopg.Error as exc:
        raise ProvenanceError(
            f"cannot reach source DB for provenance detection: {exc}"
        ) from exc

    with conn:
        with conn.cursor() as cur:
            cur.execute("SELECT current_database()")
            report.source_db_name = cur.fetchone()[0]

            cur.execute("SHOW server_version")
            ver = cur.fetchone()[0]  # "16.14 (Ubuntu ...)"
            report.postgres_version = ver.split()[0]
            report.postgres_major = _major_int(report.postgres_version)

            cur.execute(
                "SELECT pg_encoding_to_char(encoding) FROM pg_database "
                "WHERE datname = current_database()"
            )
            report.db_encoding = cur.fetchone()[0]

            # Odoo series/base version from the 'base' module row.
            cur.execute(
                "SELECT latest_version FROM ir_module_module WHERE name = 'base'"
            )
            row = cur.fetchone()
            if not row or not row[0]:
                raise ProvenanceError(
                    "source DB has no ir_module_module row for 'base' -- is this "
                    "actually an Odoo database?"
                )
            report.odoo_base_version = row[0]  # "19.0.1.3"
            report.odoo_series = _odoo_series(row[0])  # "19.0"

            # Installed modules (state='installed' only -- these are what the
            # replica must be able to load).
            cur.execute(
                "SELECT name, latest_version FROM ir_module_module "
                "WHERE state = 'installed' ORDER BY name"
            )
            report.installed_modules = [
                ModuleInfo(name=name, installed_version=ver or "")
                for name, ver in cur.fetchall()
            ]
            report.installed_module_count = len(report.installed_modules)


# ---------------------------------------------------------------------------
# Layer 2: addons on disk
# ---------------------------------------------------------------------------


def _resolve_addons_path(
    odoo_conf: str | Path | None,
    addons_path: list[str] | None,
    report: ProvenanceReport,
) -> list[str]:
    """Resolve the addons_path from an explicit list and/or an odoo.conf.

    Explicit ``addons_path`` entries and any parsed from ``odoo_conf`` are
    merged (order preserved, de-duplicated). Non-existent directories are
    dropped with a warning.
    """
    candidates: list[str] = []
    if addons_path:
        candidates.extend(addons_path)
    if odoo_conf:
        candidates.extend(_parse_addons_path_from_conf(odoo_conf, report))

    resolved: list[str] = []
    seen: set[str] = set()
    for p in candidates:
        p = p.strip()
        if not p:
            continue
        rp = str(Path(p).expanduser())
        if rp in seen:
            continue
        seen.add(rp)
        if not Path(rp).is_dir():
            report.warnings.append(f"addons_path entry does not exist: {rp}")
            continue
        resolved.append(rp)
    return resolved


def _parse_addons_path_from_conf(
    odoo_conf: str | Path, report: ProvenanceReport
) -> list[str]:
    """Extract the ``addons_path`` value from an odoo.conf (INI ``[options]``)."""
    conf_path = Path(odoo_conf).expanduser()
    if not conf_path.is_file():
        report.warnings.append(f"odoo.conf not found: {conf_path}")
        return []
    import configparser

    parser = configparser.ConfigParser()
    try:
        parser.read(conf_path)
    except configparser.Error as exc:
        report.warnings.append(f"could not parse odoo.conf {conf_path}: {exc}")
        return []
    if parser.has_option("options", "addons_path"):
        raw = parser.get("options", "addons_path")
        return [seg for seg in raw.split(",")]
    report.warnings.append(
        f"odoo.conf {conf_path} has no addons_path under [options]"
    )
    return []


def _detect_addons(addons_path: list[str], report: ProvenanceReport) -> None:
    """Walk each addons_path dir, catalog addon code, reconcile vs installed."""
    # Map addon name -> (code_path, manifest_version) for every addon dir found.
    found: dict[str, tuple[str, str | None]] = {}

    for base in addons_path:
        base_path = Path(base)
        repo = AddonRepo(path=base)
        _fill_git_info(base_path, repo)
        count = 0
        for child in sorted(base_path.iterdir()):
            if not child.is_dir():
                continue
            manifest = _read_manifest(child)
            if manifest is None:
                continue  # not an addon dir
            count += 1
            # First occurrence wins (addons_path precedence, left to right).
            found.setdefault(
                child.name, (str(child), manifest.get("version"))
            )
        repo.addon_count = count
        report.addon_repos.append(repo)

    # Reconcile: attach code_path/manifest_version to installed modules, and
    # record any installed module with no code found.
    missing: list[str] = []
    for mod in report.installed_modules:
        hit = found.get(mod.name)
        if hit:
            mod.code_path, mod.manifest_version = hit
        else:
            missing.append(mod.name)
    report.missing_addons = missing
    if missing:
        report.warnings.append(
            f"{len(missing)} installed module(s) have no code on addons_path -- "
            f"the replica cannot boot until the target provides them: "
            f"{', '.join(missing[:10])}{' ...' if len(missing) > 10 else ''}"
        )


def _read_manifest(addon_dir: Path) -> dict[str, Any] | None:
    """Parse an addon's __manifest__.py (or legacy __openerp__.py).

    Returns the manifest dict, or None if the directory isn't an addon.
    Uses ``ast.literal_eval`` -- never executes the manifest.
    """
    for name in ("__manifest__.py", "__openerp__.py"):
        mf = addon_dir / name
        if mf.is_file():
            import ast

            try:
                return ast.literal_eval(mf.read_text("utf-8"))
            except (ValueError, SyntaxError):
                return {}  # present but unparseable -> still an addon dir
    return None


def _fill_git_info(path: Path, repo: AddonRepo) -> None:
    """Best-effort git remote/commit/dirty for the repo containing ``path``."""
    if not shutil.which("git"):
        return

    def _git(*args: str) -> str | None:
        try:
            out = subprocess.run(
                ["git", "-C", str(path), *args],
                capture_output=True, text=True, check=True,
            )
            return out.stdout.strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            return None

    inside = _git("rev-parse", "--is-inside-work-tree")
    if inside != "true":
        return
    repo.git_commit = _git("rev-parse", "HEAD")
    repo.git_remote = _git("config", "--get", "remote.origin.url")
    status = _git("status", "--porcelain")
    repo.is_dirty = bool(status) if status is not None else None


# ---------------------------------------------------------------------------
# Layer 3: runtime
# ---------------------------------------------------------------------------


def _detect_runtime(odoo_bin: str, report: ProvenanceReport) -> None:
    resolved = shutil.which(odoo_bin) or (odoo_bin if Path(odoo_bin).exists() else None)
    if not resolved:
        report.warnings.append(f"--odoo-bin not found: {odoo_bin}")
        return
    try:
        out = subprocess.run(
            [resolved, "--version"], capture_output=True, text=True, check=True
        )
        report.odoo_bin_version = out.stdout.strip() or out.stderr.strip()
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        report.warnings.append(f"could not run '{odoo_bin} --version': {exc}")

    # The Python behind this odoo-bin: read its shebang, else skip (we do NOT
    # substitute the tool's own interpreter -- that would misreport the source).
    py = _python_behind(resolved)
    if py:
        report.python_version = py
    else:
        report.warnings.append(
            f"could not determine the Python version behind {resolved}"
        )


def _python_behind(odoo_bin: str) -> str | None:
    """Resolve the Python interpreter an odoo-bin runs under, from its shebang."""
    try:
        first_line = Path(odoo_bin).read_text("utf-8", errors="ignore").splitlines()[0]
    except (OSError, IndexError):
        return None
    if not first_line.startswith("#!"):
        return None
    interp = first_line[2:].strip().split()
    # Handle "/usr/bin/env python3" and direct interpreter paths alike.
    exe = interp[-1] if interp and interp[0].endswith("env") else (interp[0] if interp else "")
    if not exe:
        return None
    resolved = shutil.which(exe) or (exe if Path(exe).exists() else None)
    if not resolved:
        return None
    try:
        out = subprocess.run(
            [resolved, "--version"], capture_output=True, text=True, check=True
        )
        return (out.stdout or out.stderr).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _major_int(version: str) -> int:
    """"16.14" -> 16; "18beta" -> 18; "" -> 0."""
    m = re.match(r"(\d+)", version or "")
    return int(m.group(1)) if m else 0


def _odoo_series(base_version: str) -> str:
    """"19.0.1.3" -> "19.0"; falls back to the raw value if it can't split."""
    parts = (base_version or "").split(".")
    if len(parts) >= 2 and parts[0].isdigit():
        return f"{parts[0]}.{parts[1]}"
    return base_version or ""


def content_hash_of_dir(path: str | Path) -> str:
    """Stable sha256 of a directory's *.py/manifest content (name + bytes).

    Used by the replica preflight to compare an addon's on-disk code against
    the source. Deterministic across machines: walks files in sorted order,
    hashing the repo-relative path plus the file bytes. Ignores VCS metadata
    and Python bytecode caches so a checkout and its source compare equal.
    """
    base = Path(path)
    h = hashlib.sha256()
    for f in sorted(base.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(base).as_posix()
        if "/.git/" in f"/{rel}/" or rel.startswith(".git/"):
            continue
        if "__pycache__/" in rel or rel.endswith(".pyc"):
            continue
        h.update(rel.encode("utf-8"))
        h.update(b"\x00")
        h.update(f.read_bytes())
        h.update(b"\x00")
    return h.hexdigest()
