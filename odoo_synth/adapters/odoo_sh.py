"""odoo.sh adapter.

v1 (P1 item #10): ingest() validates a manually-downloaded backup zip's
manifest.json (odoo-version and module list) and unzips it into a
restorable bundle, failing loudly on any mismatch.

v2 (explicitly deferred per AGENT_PROMPT.md): pull_via_ssh() -- SSH
automation to pull a backup directly from odoo.sh. This is a stub that
raises NotImplementedError; do NOT build it now.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path


class IngestError(Exception):
    """Raised when an odoo.sh backup zip fails validation."""


def ingest(zip_path: str | Path, out_dir: str | Path) -> Path:
    """TODO P1 #10: validate and unzip a manually-downloaded odoo.sh backup.

    Will validate manifest.json's odoo-version (must be 19.x) and module
    list, fail loudly on mismatch, then unzip into out_dir. The actual
    extraction + validation is not yet wired into the CLI.
    """
    zip_path = Path(zip_path)
    if not zip_path.exists():
        raise IngestError(f"backup zip not found: {zip_path}")
    # Peek at manifest.json without extracting, to surface the v1 contract.
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            manifest_names = [n for n in names if n.endswith("manifest.json")]
            if not manifest_names:
                raise IngestError(
                    f"{zip_path}: no manifest.json found -- not a valid "
                    "odoo.sh backup zip"
                )
            with zf.open(manifest_names[0]) as mf:
                manifest = json.load(mf)
    except zipfile.BadZipFile as exc:
        raise IngestError(f"{zip_path}: not a valid zip file: {exc}") from exc

    version = str(manifest.get("odoo-version", "") or "")
    if not version.startswith("19"):
        raise IngestError(
            f"{zip_path}: manifest odoo-version is `{version}`, expected a "
            "19.x backup (this tool targets Odoo v19)."
        )
    raise NotImplementedError(
        "odoo_sh.ingest validated the manifest but full extraction into a "
        "restorable bundle is not implemented yet -- P1 item #10. The "
        f"backup targets Odoo {version}; wire the unzip + bundle packaging "
        "step to complete this adapter."
    )


def pull_via_ssh() -> None:
    """v2 (deferred): pull an odoo.sh backup over SSH. Do not build now."""
    raise NotImplementedError(
        "odoo_sh.pull_via_ssh is intentionally a stub -- AGENT_PROMPT.md "
        "defers SSH backup automation to v2. v1 ingests a manually-"
        "downloaded backup zip via ingest() instead."
    )
