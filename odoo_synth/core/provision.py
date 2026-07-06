"""Provision a fresh Odoo instance from a masked bundle.

GUARDRAIL (from AGENT_PROMPT.md): must call `odoo-bin db load --neutralize`
(or equivalent). Never boot a provisioned instance without --neutralize --
that's what disables outgoing mail, cron, and payment providers at runtime
on top of the credential scrubbing the rulebook already did.

P1 item #11 -- not implemented yet.
"""

from __future__ import annotations

from pathlib import Path


def provision(bundle: Path) -> None:
    """TODO P1 #11: restore a masked bundle and launch a neutralized Odoo.

    Steps when implemented:
      1. pg_restore the bundle's db.dump into a fresh Postgres database.
      2. `odoo-bin db load --neutralize` against that database.
      3. Launch the Odoo container pointed at it.
    """
    raise NotImplementedError(
        "provision.provision is not implemented yet -- P1 item #11. Per "
        "the guardrails it will call `odoo-bin db load --neutralize` before "
        "booting the instance -- a provisioned instance must never boot "
        "without --neutralize."
    )
