"""Self-hosted Odoo adapter: odoo-bin db dump/load if available, else
pg_dump -Fd -j N + filestore rsync.

P1 item #9 -- not implemented yet. The plan:

  * detect whether `odoo-bin` is on PATH; if so, prefer `odoo-bin db dump`
    (which produces a zip with the DB and filestore together) and
    `odoo-bin db load` on the provision side.
  * otherwise fall back to `pg_dump -Fd -j N <db>` for the DB and rsync the
    filestore directory separately. This needs the Odoo config's data_dir /
    filestore path, which the caller must supply.
"""

from __future__ import annotations

from pathlib import Path


def dump(db: str, out: Path) -> Path:
    """TODO P1 #9: dump a self-hosted Odoo DB to a restorable artifact."""
    raise NotImplementedError(
        "self_hosted.dump is not implemented yet -- P1 item #9. It will "
        "use `odoo-bin db dump` if odoo-bin is on PATH, else "
        "`pg_dump -Fd -j N` + an rsync of the filestore directory."
    )


def load(artifact: Path, db: str) -> None:
    """TODO P1 #9: restore a dumped artifact into a fresh database."""
    raise NotImplementedError(
        "self_hosted.load is not implemented yet -- P1 item #9."
    )
