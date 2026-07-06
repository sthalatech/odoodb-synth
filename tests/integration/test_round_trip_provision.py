"""Layer 4 (TESTING.md section 4): full round-trip smoke test.

Provision the masked snapshot as a fresh Odoo instance and confirm it's
usable (HTTP 200 on login page, login works, a smoke action renders).
Needs the full compose stack plus a completed snapshot. P1 item #12 --
not implemented; skipped unless ODOO_SYNTH_RUN_INTEGRATION=1.
"""

from __future__ import annotations

import os
import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("ODOO_SYNTH_RUN_INTEGRATION") != "1",
    reason="integration tests need the full compose stack; set "
    "ODOO_SYNTH_RUN_INTEGRATION=1 to run",
)


def test_provisioned_instance_is_usable():
    """TODO P1 #12: provision a masked bundle and smoke-test through the UI.

    Checks (from TESTING.md section 4):
      * Instance boots and serves HTTP 200 on the login page.
      * Login works via the reset-password path provision.py establishes.
      * One or two JSON-RPC/UI actions succeed (open a customer, open an
        invoice) -- confirms masked data renders, not just passes SQL.
    """
    raise NotImplementedError(
        "round-trip provision smoke test is not implemented yet -- P1 "
        "item #12 / #11 (core/provision.py)."
    )
