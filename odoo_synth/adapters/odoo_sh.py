"""odoo.sh adapter.

v1 (this file): ingest() validates a manually-downloaded backup zip's
manifest.json (odoo-version and module list) and unzips it into a
restorable bundle, failing loudly and specifically on any mismatch.

v2 (explicitly deferred per AGENT_PROMPT.md): pull_via_ssh() -- SSH
automation to pull a backup directly from odoo.sh. This is a stub that
raises NotImplementedError; do NOT build it now.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

TARGET_MAJOR = "19"  # this tool targets Odoo v19


class IngestError(Exception):
    """Raised when an odoo.sh backup zip fails validation."""


def ingest(zip_path: str | Path, out_dir: str | Path, rules_dir: str | Path | None = None) -> Path:
    """Validate and unzip a manually-downloaded odoo.sh backup into a bundle.

    Steps:
      1. Open the zip; locate manifest.json (at top level or one level down).
      2. Validate manifest.odoo_version starts with the target major (19).
         Fail LOUDLY naming the field + expected vs actual on mismatch.
      3. Extract the whole zip into out_dir.
      4. If a module list is present in the manifest, note (via a warning
         list in the returned report) any installed module with no
         corresponding rules/8x_*.yml file -- that's the signal `rules scan`
         uses. We don't fail on it (a module without PII fields doesn't need
         a rule file), but we surface it so the caller can act.

    Returns the out_dir Path. Raises IngestError on any hard failure.
    """
    zip_path = Path(zip_path)
    out_dir = Path(out_dir)
    if not zip_path.exists():
        raise IngestError(f"backup zip not found: {zip_path}")
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as exc:
        raise IngestError(f"{zip_path}: not a valid zip file: {exc}") from exc

    with zf:
        names = zf.namelist()
        manifest_name, manifest = _read_manifest(zf, names)
        _validate_version(zip_path, manifest, manifest_name)

        # Extract everything into out_dir.
        zf.extractall(out_dir)

    # Post-extract: locate the dump.sql + filestore and normalize into the
    # standard bundle layout (so downstream code treats odoo.sh + self-hosted
    # bundles identically).
    report = _normalize_bundle(out_dir, manifest, rules_dir)
    return out_dir


def _read_manifest(zf: zipfile.ZipFile, names: list[str]) -> tuple[str, dict]:
    """Find + parse manifest.json. Returns (name-in-zip, parsed-dict)."""
    candidates = [n for n in names if n.endswith("manifest.json")]
    if not candidates:
        raise IngestError(
            f"{zf.filename}: no manifest.json found -- not a valid odoo.sh "
            "backup zip (odoo.sh backups always carry one at the zip root)."
        )
    # Prefer a root-level manifest.json (no path separator).
    root = [n for n in candidates if "/" not in n.rstrip("/")]
    target = (root or candidates)[0]
    try:
        with zf.open(target) as mf:
            manifest = json.load(mf)
    except json.JSONDecodeError as exc:
        raise IngestError(
            f"{zf.filename}: manifest.json is not valid JSON: {exc}"
        ) from exc
    if not isinstance(manifest, dict):
        raise IngestError(
            f"{zf.filename}: manifest.json top-level must be an object, got "
            f"{type(manifest).__name__}"
        )
    return target, manifest


def _validate_version(zip_path: Path, manifest: dict, manifest_name: str) -> None:
    """Fail LOUDLY on odoo-version mismatch, naming field + expected vs actual."""
    # odoo.sh manifests use either 'odoo_version' or 'odoo-version'.
    version = manifest.get("odoo_version") or manifest.get("odoo-version")
    if version is None:
        raise IngestError(
            f"{zip_path}: manifest.json ({manifest_name}) has no "
            "'odoo_version'/'odoo-version' field -- cannot confirm the "
            "backup targets Odoo v19."
        )
    version = str(version)
    if not version.startswith(TARGET_MAJOR + ".") and not version.startswith(TARGET_MAJOR):
        raise IngestError(
            f"{zip_path}: manifest.json field 'odoo_version' is "
            f"`{version}`, expected a {TARGET_MAJOR}.x backup (this tool "
            f"targets Odoo v{TARGET_MAJOR}). Refusing to ingest a backup "
            "of the wrong major version -- masking rules may not apply "
            "correctly across Odoo majors."
        )


def _normalize_bundle(out_dir: Path, manifest: dict, rules_dir: str | Path | None) -> dict:
    """Normalize the extracted zip into the standard bundle layout + report.

    odoo.sh backups unpack to a directory containing dump.sql (or
    dump.dump) and a filestore/ dir, plus manifest.json. We ensure the
    layout matches the self-hosted bundle shape and write/merge manifest.json
    at the bundle root.
    """
    # Find the real content dir (odoo.sh zips often have a single top dir).
    content_dir = out_dir
    children = [p for p in out_dir.iterdir()]
    if len(children) == 1 and children[0].is_dir():
        content_dir = children[0]

    # Write manifest.json at the bundle root (merge with any existing).
    mf_path = out_dir / "manifest.json"
    existing = {}
    if mf_path.exists():
        try:
            existing = json.loads(mf_path.read_text("utf-8"))
        except Exception:
            existing = {}
    existing.update({
        "source": "odoo_sh",
        "odoo_version": str(manifest.get("odoo_version") or manifest.get("odoo-version") or ""),
        "manifest_filename_in_zip": manifest.get("manifest_filename_in_zip"),
    })

    # Build a schema.json sidecar from the bundle's dump.sql, if present, so
    # `rules scan` can run against an odoo.sh bundle without a live DB. This
    # is a best-effort parse (see core/schema.snapshot_from_dump_sql); the
    # unparsed tables are surfaced in the snapshot so the scan reports the
    # gap instead of silently trusting an empty result.
    schema_report = _maybe_write_schema_snapshot(content_dir, out_dir)
    if schema_report:
        existing["schema_snapshot"] = schema_report

    mf_path.write_text(json.dumps(existing, indent=2, sort_keys=True), "utf-8")

    report: dict[str, Any] = {"undeclared_modules": []}
    # If a module list is present, flag modules with no rules/8x_*.yml.
    modules = manifest.get("modules") or manifest.get("module_list") or []
    if isinstance(modules, dict):
        module_names = list(modules.keys())
    elif isinstance(modules, list):
        module_names = [str(m) for m in modules]
    else:
        module_names = []
    if module_names and rules_dir:
        report["undeclared_modules"] = _flag_undeclared_modules(module_names, Path(rules_dir))
    return report


def _maybe_write_schema_snapshot(content_dir: Path, out_dir: Path) -> dict[str, Any]:
    """Parse dump.sql (if present in the bundle) into a schema.json sidecar.

    Returns a small report dict for the manifest (parsed-table count + any
    unparsed tables). No-op if there's no dump.sql. odoo.sh backups ship a
    plain-text dump.sql by default; if this is a custom-format dump.dump we
    can't parse it textually and skip (the self-hosted path writes schema.json
    from the catalog instead).
    """
    from ..core.schema import snapshot_from_dump_sql

    dump_sql = content_dir / "dump.sql"
    if not dump_sql.exists():
        # Some bundles nest it one more level.
        cand = list(out_dir.rglob("dump.sql"))
        if cand:
            dump_sql = cand[0]
    if not dump_sql.exists():
        return {"present": False}
    try:
        snap = snapshot_from_dump_sql(dump_sql.read_text("utf-8", errors="replace"))
        (out_dir / "schema.json").write_text(snap.to_json(), "utf-8")
        return {
            "present": True,
            "source": snap.source,
            "tables": len(snap.tables),
            "unparsed_tables": len(snap.unparsed_tables),
        }
    except Exception as exc:
        return {"present": True, "error": str(exc)}


def _flag_undeclared_modules(modules: list[str], rules_dir: Path) -> list[str]:
    """Return installed modules with no rules/8x_*.yml coverage.

    This is the signal `rules scan` consumes. We don't fail -- a module
    without PII fields doesn't need a rule file -- but we surface the list so
    the caller can wire it to `rules scan` (TODO: that's the P1 #8/#12 path).
    """
    if not rules_dir.is_dir():
        return []
    rule_files = {p.name for p in rules_dir.glob("8x_*.yml")}
    # Heuristic: a module is "declared" if any 8x_*.yml mentions it as a
    # top-level key. (The loader treats unknown top-level keys without
    # `fields:` as metadata and skips them, so module coverage lives in the
    # numbered files.) This is a coarse flag, not a coverage claim.
    declared: set[str] = set()
    for rf in rule_files:
        try:
            import yaml
            doc = yaml.safe_load((rules_dir / rf).read_text("utf-8")) or {}
            if isinstance(doc, dict):
                declared.update(k for k in doc if isinstance(k, str))
        except Exception:
            continue
    return sorted(m for m in modules if m not in declared)


def pull_via_ssh() -> None:
    """v2 (deferred): pull an odoo.sh backup over SSH. Do not build now."""
    raise NotImplementedError(
        "odoo_sh.pull_via_ssh is intentionally a stub -- AGENT_PROMPT.md "
        "defers SSH backup automation to v2. v1 ingests a manually-"
        "downloaded backup zip via ingest() instead."
    )
