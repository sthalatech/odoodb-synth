"""Layer 3 (TESTING.md section 3): rule-application integration test.

Needs Docker: the postgres-anon service AND an Odoo 19 instance with demo
data loaded (docker/docker-compose.scratch.yml up -d, then init the Odoo
DB). Not in the default CI unit workflow -- run manually or in a nightly
job (P1 item #12).

Skipped unless ODOO_SYNTH_RUN_INTEGRATION=1 is set, so importing this
module in a unit-only environment is a no-op.
"""

from __future__ import annotations

import os
import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("ODOO_SYNTH_RUN_INTEGRATION") != "1",
    reason="integration tests need both containers up; set "
    "ODOO_SYNTH_RUN_INTEGRATION=1 and run "
    "`docker compose -f docker/docker-compose.scratch.yml up -d` first",
)


def test_snapshot_masks_demo_data():
    """TODO P1 #12: full snapshot pipeline against Odoo 19 demo data.

    Acceptance gates (from TESTING.md section 3):
      * Odoo still boots against the masked DB
        (`odoo-bin -d masked_db -u all --stop-after-init` exits 0).
      * Leak scan: before masking, dump res.partner.name/email,
        res.users.login, hr.employee.name; after masking, pg_dump to plain
        SQL text and grep -F for every original value. Zero matches.
      * mail.tracking.value: count of rows with non-null old/new_value_char
        is 0 after masking.
      * payment_token.provider_ref IS NULL and payment_provider.state =
        'disabled' for all rows.
      * ir.config_parameter database.secret and database.uuid differ from
        the source DB's values (actually different, not just non-null).
    """
    raise NotImplementedError(
        "end-to-end integration test is not implemented yet -- P1 item #12. "
        "It boots Odoo 19 with demo data, runs the full snapshot pipeline, "
        "and runs the leak-scan + structural checks from TESTING.md "
        "section 3 against the output."
    )
