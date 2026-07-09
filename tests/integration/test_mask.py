"""Mask-application test (TESTING.md section 3, small-scale).

Applies the full rulebook to a hand-seeded scratch schema and asserts the
known PII values are gone -- the same leak-scan logic TESTING.md describes
at database scale, written small and fast. Gated on the scratch Postgres
being up (postgres-anon service) but NOT on the full integration env flag:
this runs in CI alongside the SQL function tests, because it only needs the
postgres-anon container, not Odoo.

Creates a throwaway database per test run (dropped afterward) so it never
touches the shared scratch DB the SQL unit tests use.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RULES_DIR = REPO_ROOT / "rules"

pytestmark = pytest.mark.skipif(
    os.environ.get("ODOO_SYNTH_SKIP_MASK_TEST") == "1",
    reason="ODOO_SYNTH_SKIP_MASK_TEST=1 set",
)


def _can_connect(db_url: str) -> bool:
    try:
        import psycopg
        with psycopg.connect(db_url, connect_timeout=3) as c, c.cursor() as cur:
            cur.execute("SELECT 1")
        return True
    except Exception:
        return False


@pytest.fixture(scope="module")
def admin_url():
    """URL to the scratch Postgres with admin (CREATE DATABASE) rights."""
    # The compose postgres-anon service uses odoo_synth/odoo_synth, which is
    # the superuser in that container, so it can CREATE DATABASE.
    url = os.environ.get("SCRATCH_DB_URL")
    if not url:
        pytest.skip("SCRATCH_DB_URL unset; start postgres-anon via "
                    "`docker compose -f docker/docker-compose.scratch.yml up -d postgres-anon`")
    if not _can_connect(url):
        pytest.skip(f"cannot connect to SCRATCH_DB_URL={url}")
    return url


@pytest.fixture
def isolated_db(admin_url):
    """A fresh throwaway database for one test, dropped after."""
    import psycopg
    dbname = "masktest_" + uuid.uuid4().hex[:12]
    # Connect to the maintenance/default DB to CREATE DATABASE.
    # admin_url points at `scratch`; odoo_synth user can create dbs.
    with psycopg.connect(admin_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{dbname}"')
    test_url = admin_url.rsplit("/", 1)[0] + "/" + dbname
    try:
        # Load the odoo_synth bootstrap functions (the fresh DB doesn't have
        # them -- init scripts only ran on the `scratch` DB).
        with psycopg.connect(test_url, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute((REPO_ROOT / "sql" / "bootstrap.sql").read_text("utf-8"))
        yield test_url
    finally:
        # Drop with FORCE so open connections don't block it.
        with psycopg.connect(admin_url, autocommit=True) as conn:
            with conn.cursor() as cur:
                conn.autocommit = True
                cur.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')


# Tables this fixture creates -- dropped at the start of _seed so the
# scratch DB stays clean without dropping the `public` schema (which would
# also drop the `anon` extension, since anon is registered against public).
_SEED_TABLES = [
    "res_partner", "ir_config_parameter", "payment_token", "payment_provider",
    "mail_tracking_value", "ir_attachment", "hr_employee", "mail_message",
]


def _seed(cur):
    """Create Odoo-shaped tables + seed known PII values. Returns the
    dict of original values the leak scan must NOT find afterward."""
    # Drop only the tables we own, preserving the anon extension + its
    # odoo_synth helper schema.
    for tbl in _SEED_TABLES:
        cur.execute(f'DROP TABLE IF EXISTS "{tbl}" CASCADE')
    # res.partner
    cur.execute("""
        CREATE TABLE res_partner (
            id serial PRIMARY KEY, name text, email text, phone text,
            street text, city text, zip text, vat text, comment text,
            image_1920 bytea, partner_latitude double precision,
            partner_longitude double precision, barcode text
        )
    """)
    cur.execute("""
        INSERT INTO res_partner (name,email,phone,street,city,zip,vat,comment,
                                 image_1920,partner_latitude,partner_longitude,barcode)
        VALUES ('Alice Acme','alice@acme-corp-real.com','+32 470 12 34 56',
                '12 Real Street','Realville','1000','BE0123456789',
                'Alice private note here',
                decode('deadbeef','hex'), 50.8466, 4.3528, '0420000012345')
    """)
    # ir.config_parameter -- secret-shaped keys + named rotate keys
    cur.execute("""
        CREATE TABLE ir_config_parameter (
            id serial PRIMARY KEY, key text, value text
        )
    """)
    cur.execute("""
        INSERT INTO ir_config_parameter (key,value) VALUES
            ('database.secret','SRC_SECRET_abcdef123456'),
            ('database.uuid','11111111-1111-1111-1111-111111111111'),
            ('some_api_key','AKIAIOSFODNN7EXAMPLE'),
            ('mail.catchall.domain','example.com')
    """)
    # payment.token / payment.provider
    cur.execute("""
        CREATE TABLE payment_token (
            id serial PRIMARY KEY, payment_details text, provider_ref text,
            active boolean
        );
        INSERT INTO payment_token VALUES
            (1,'**** 4242','live_token_xyz789', true)
    """)
    cur.execute("""
        CREATE TABLE payment_provider (
            id serial PRIMARY KEY, state text, stripe_secret_key text
        );
        INSERT INTO payment_provider VALUES (1,'enabled','sk_live_realkey')
    """)
    # mail.tracking.value -- the chatter-history gotcha
    cur.execute("""
        CREATE TABLE mail_tracking_value (
            id serial PRIMARY KEY, old_value_char text, new_value_char text
        );
        INSERT INTO mail_tracking_value VALUES
            (1,'alice@acme-corp-real.com','alice2@acme-corp-real.com'),
            (2,'+32 470 12 34 56','+32 470 99 88 77')
    """)
    # ir.attachment -- with a full-scrub-model row (res.partner)
    cur.execute("""
        CREATE TABLE ir_attachment (
            id serial PRIMARY KEY, name text, datas bytea, store_fname text,
            checksum text, index_content text, res_model text
        );
        INSERT INTO ir_attachment VALUES
            (1,'John_Smith_Resume.pdf', decode('cafebabe','hex'),
             'filestore/ab/cd/abcdef', 'sha1fake', 'extracted text content',
             'res.partner'),
            (2,'invoice_001.pdf', decode('beefdead','hex'),
             'filestore/ef/gh/efghij', 'sha1fake2', 'invoice content',
             'account.move')
    """)
    # hr.employee (for the pin rotate + birthday generalize)
    cur.execute("""
        CREATE TABLE hr_employee (
            id serial PRIMARY KEY, name text, work_email text, birthday date,
            pin text, km_home_work numeric
        );
        INSERT INTO hr_employee VALUES
            (1,'Bob Builder','bob@realemail.com','1985-06-15','9999', 42.5)
    """)
    # mail.message (email_from fake_email)
    cur.execute("""
        CREATE TABLE mail_message (
            id serial PRIMARY KEY, body text, subject text, email_from text
        );
        INSERT INTO mail_message VALUES
            (1,'chatter about Alice','Re: Alice billing','alice@acme-corp-real.com')
    """)
    return {
        "name": "Alice Acme",
        "email": "alice@acme-corp-real.com",
        "phone": "+32 470 12 34 56",
        "street": "12 Real Street",
        "city": "Realville",
        "zip": "1000",
        "vat": "BE0123456789",
        "comment": "Alice private note here",
        "barcode": "0420000012345",
        "secret": "SRC_SECRET_abcdef123456",
        "uuid_src": "11111111-1111-1111-1111-111111111111",
        "api_key": "AKIAIOSFODNN7EXAMPLE",
        "token_ref": "live_token_xyz789",
        "token_details": "**** 4242",
        "stripe_key": "sk_live_realkey",
        "tracking_old": "alice@acme-corp-real.com",
        "tracking_new": "alice2@acme-corp-real.com",
        "tracking_phone_old": "+32 470 12 34 56",
        "tracking_phone_new": "+32 470 99 88 77",
        "attachment_name": "John_Smith_Resume.pdf",
        "attachment_content_marker": "extracted text content",
        "hr_name": "Bob Builder",
        "hr_email": "bob@realemail.com",
        "hr_pin": "9999",
        "msg_email_from": "alice@acme-corp-real.com",
    }


def test_apply_masking_kills_all_known_pii(isolated_db):
    """Apply the full rulebook and assert every seeded PII value is gone.

    This is the leak scan from TESTING.md section 3, on a tiny seeded schema.
    The masked DB is pg_dumped to text and every original value is grepped for
    -- zero matches is the pass bar, exactly like the full integration gate.
    """
    from odoo_synth.core.rulebook import load_and_validate
    from odoo_synth.core.mask import apply_masking, leak_scan

    rb = load_and_validate(RULES_DIR)

    import psycopg
    with psycopg.connect(isolated_db, autocommit=True) as conn:
        with conn.cursor() as cur:
            originals = _seed(cur)

    summary = apply_masking(isolated_db, rb)
    assert summary["anonymize_database"] is not None or summary["labels_applied"] > 0

    # Reconnect read-only and assert the column-level outcomes directly --
    # independent of the leak-scan dump (two checks, not one).
    with psycopg.connect(isolated_db, autocommit=True) as conn:
        with conn.cursor() as cur:
            # res.partner fields masked.
            cur.execute("SELECT name,email,phone,vat,comment,barcode,"
                        "partner_latitude,partner_longitude FROM res_partner WHERE id=1")
            r = cur.fetchone()
            assert r[0] != originals["name"], f"name leaked: {r[0]}"
            assert r[1] != originals["email"] and "@example.invalid" in r[1], f"email: {r[1]}"
            assert r[2] != originals["phone"], f"phone leaked: {r[2]}"
            assert r[3] != originals["vat"], f"vat leaked: {r[3]}"
            assert r[4] != originals["comment"], f"comment leaked: {r[4]}"
            assert r[5] != originals["barcode"], f"barcode leaked: {r[5]}"
            assert r[6] is None and r[7] is None, "lat/long not dropped"

            # ir.config_parameter: secret-shaped keys nulled, database.secret
            # + database.uuid rotated to fresh values (different from source).
            cur.execute("SELECT key,value FROM ir_config_parameter ORDER BY key")
            params = {k: v for (k, v) in cur.fetchall()}
            assert params["some_api_key"] is None, "regex-keyed api_key not nulled"
            assert params["database.secret"] is not None
            assert params["database.secret"] != originals["secret"], "secret not rotated"
            assert params["database.uuid"] is not None
            assert params["database.uuid"] != originals["uuid_src"], "uuid not rotated"
            # mail.catchall.domain does NOT match the regex -> kept.
            assert params["mail.catchall.domain"] == "example.com", \
                "non-secret config param should be kept, not nulled"

            # payment.token: provider_ref dropped, active forced false.
            cur.execute("SELECT payment_details,provider_ref,active FROM payment_token WHERE id=1")
            tk = cur.fetchone()
            assert tk[0] is None, "payment_details not dropped"
            assert tk[1] is None, "provider_ref not dropped"
            assert tk[2] is False, "active not forced false"

            # payment.provider: state disabled, secret key dropped.
            cur.execute("SELECT state,stripe_secret_key FROM payment_provider WHERE id=1")
            pv = cur.fetchone()
            assert pv[0] == "disabled", f"provider state={pv[0]}"
            assert pv[1] is None, "stripe_secret_key not dropped"

            # mail.tracking.value: both value cols NULL.
            cur.execute("SELECT count(*) FROM mail_tracking_value "
                        "WHERE old_value_char IS NOT NULL OR new_value_char IS NOT NULL")
            assert cur.fetchone()[0] == 0, "mail_tracking_value values not nulled"

            # ir.attachment: content/index nulled, res.partner filename scrubbed,
            # account.move filename kept (not in scrub list).
            cur.execute("SELECT name,datas,store_fname,checksum,index_content,res_model "
                        "FROM ir_attachment ORDER BY id")
            atts = cur.fetchall()
            a1 = atts[0]  # res.partner -> full scrub
            assert a1[0] != originals["attachment_name"], "attachment filename not scrubbed"
            assert a1[0].startswith("attachment_1"), f"scrubbed name wrong: {a1[0]}"
            assert a1[1] is None and a1[2] is None and a1[3] is None and a1[4] is None, \
                "attachment content not dropped"
            a2 = atts[1]  # account.move -> filename kept, content dropped
            assert a2[0] == "invoice_001.pdf", "account.move filename should be kept"
            assert a2[1] is None and a2[4] is None, "attachment content not dropped"

            # hr.employee: name/email masked, birthday generalized to Jan 1,
            # pin rotated to the dev default.
            cur.execute("SELECT name,work_email,birthday,pin FROM hr_employee WHERE id=1")
            h = cur.fetchone()
            assert h[0] != originals["hr_name"], "hr name leaked"
            assert h[1] != originals["hr_email"], "hr email leaked"
            assert str(h[2]) == "1985-01-01", f"birthday not generalized: {h[2]}"
            assert h[3] != originals["hr_pin"], "pin not rotated"

            # mail.message: email_from masked, body redacted.
            cur.execute("SELECT subject,email_from FROM mail_message WHERE id=1")
            m = cur.fetchone()
            assert m[1] != originals["msg_email_from"], "msg email_from leaked"

    # Leak scan: pg_dump the whole masked DB to text and grep for every
    # original value. Zero substring matches required -- this is the real
    # TESTING.md section 3 gate, on a small schema.
    leaked = leak_scan(isolated_db, list(originals.values()))
    assert leaked == [], f"leak scan found original values in masked dump: {leaked}"


def test_apply_masking_skips_missing_tables_gracefully(isolated_db):
    """An instance missing a module's tables must not abort the whole pass.

    The plan references payment_token etc.; if those tables don't exist, the
    labels for them are skipped (counted), not raised. This is the normal
    case across Odoo instances with different module sets.
    """
    from odoo_synth.core.rulebook import load_and_validate
    from odoo_synth.core.mask import apply_masking
    rb = load_and_validate(RULES_DIR)

    import psycopg
    with psycopg.connect(isolated_db, autocommit=True) as conn:
        with conn.cursor() as cur:
            # Only res_partner exists; everything else in the rulebook is absent.
            cur.execute("DROP TABLE IF EXISTS res_partner CASCADE")
            cur.execute("CREATE TABLE res_partner (id serial PRIMARY KEY, name text, email text)")
            cur.execute("INSERT INTO res_partner (name,email) VALUES ('X','x@y.com')")

    summary = apply_masking(isolated_db, rb)
    # Many labels skipped (124 total plan - the ~2 res_partner ones applied).
    assert summary["labels_skipped"] > 0, "expected skipped labels for missing tables"
    assert summary["labels_applied"] >= 2, "res_partner labels should apply"


def test_rotate_secret_rule_without_implementation_fails_loud(isolated_db):
    """A rotate_secret rule with no _ROTATE_SECRET_SQL entry is a hard error.

    Honest-failure contract: an undeclared rotate implementation is a real
    gap, not something to silently skip -- masking would be incomplete.
    """
    from odoo_synth.core.rulebook import load_and_validate, Rulebook, Strategy, FieldRule
    from odoo_synth.core.mask import apply_masking, MaskError
    import psycopg
    # Build a minimal rulebook with one rotate_secret rule for a model/field
    # that has no implementation, on a table that DOES exist (so the
    # missing-implementation check is what fires, not the missing-table skip).
    rb = Rulebook()
    rb.strategies = {"rotate_secret": Strategy(name="rotate_secret", sql_template=None)}
    rb.raw = {}
    rb.field_rules = {("my.model", "myfield"): FieldRule(
        model="my.model", field="myfield", strategy="rotate_secret", file="t.yml"
    )}
    with psycopg.connect(isolated_db, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE TABLE my_model (id serial PRIMARY KEY, myfield text)")
    with pytest.raises(MaskError, match="no implementation"):
        apply_masking(isolated_db, rb)


def test_leak_scan_catches_unmasked_values(isolated_db):
    """Negative test: leak_scan MUST find PII that wasn't masked.

    A pass on the masked DB (test_apply_masking_kills_all_known_pii) is only
    meaningful if the scan can actually detect leaks. This seeds an unmasked
    value and confirms the scan catches it -- guards against a false-pass
    where the scan silently scans the wrong schemas/tables.
    """
    from odoo_synth.core.mask import leak_scan
    import psycopg
    with psycopg.connect(isolated_db, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS leak_canary CASCADE")
            cur.execute("CREATE TABLE leak_canary (id serial PRIMARY KEY, secret text)")
            cur.execute("INSERT INTO leak_canary (secret) VALUES ('UNMASKED_CANARY_VALUE')")
    leaked = leak_scan(isolated_db, ["UNMASKED_CANARY_VALUE", "ABSENT_VALUE"])
    assert "UNMASKED_CANARY_VALUE" in leaked, (
        "leak_scan failed to find an unmasked value -- a pass on masked data "
        "would be a false negative (scanning the wrong schemas/tables)."
    )
    assert "ABSENT_VALUE" not in leaked, "leak_scan reported a value not in the DB"
    # cleanup
    with psycopg.connect(isolated_db, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS leak_canary CASCADE")


# ---------------------------------------------------------------------------
# Column-name pattern masking (05_patterns.yml backstop)
# ---------------------------------------------------------------------------

_PATTERN_SEED_TABLES = ["res_partner", "account_move", "x_random_doc"]


def _seed_pattern(cur):
    """Seed an undeclared model x_random_doc with a *_display_name cache of a
    partner name, plus account_move (declared) whose invoice_partner_display_name
    is covered by an EXPLICIT rule (not the pattern). Verifies the pattern
    backstop masks the undeclared cache, and the explicit rule wins on the
    declared model."""
    for tbl in _PATTERN_SEED_TABLES:
        cur.execute(f'DROP TABLE IF EXISTS "{tbl}" CASCADE')
    cur.execute("""
        CREATE TABLE res_partner (
            id serial PRIMARY KEY, name text, email text
        )
    """)
    cur.execute("INSERT INTO res_partner (name,email) VALUES "
                "('Real Company Inc','real@company-real.com')")
    # account_move: declared in 20_accounting.yml. invoice_partner_display_name
    # has an EXPLICIT redact_freetext rule -- must be masked by the explicit
    # label, and must NOT appear as a pattern label (explicit wins).
    cur.execute("""
        CREATE TABLE account_move (
            id serial PRIMARY KEY, name text, ref text, narration text,
            invoice_partner_display_name text, invoice_source_email text
        )
    """)
    cur.execute("INSERT INTO account_move (name, ref, narration, "
                "invoice_partner_display_name, invoice_source_email) VALUES "
                "('INV/2024/0001', 'PO-12345', 'billing note', "
                "'Real Company Inc', 'vendor@company-real.com')")
    # x_random_doc: UNDECLARED model carrying a *_display_name cache. Only the
    # pattern backstop can mask this -- no explicit rule exists for it.
    cur.execute("""
        CREATE TABLE x_random_doc (
            id serial PRIMARY KEY,
            some_display_name text,
            complete_name text,
            ref text
        )
    """)
    cur.execute("INSERT INTO x_random_doc (some_display_name, complete_name, ref) "
                "VALUES ('Real Company Inc', 'Real Company Inc - child', 'note')")
    return {"cache_name": "Real Company Inc",
            "cache_complete": "Real Company Inc - child",
            "vendor_email": "vendor@company-real.com"}


def test_pattern_labels_mask_undeclared_cache_columns(isolated_db):
    """The 05_patterns.yml backstop must mask *_display_name / complete_name
    cache columns on UNDECLARED models (where no explicit field rule exists),
    closing the denormalized-cache leak the v0.1.0 darkstore run found."""
    from odoo_synth.core.rulebook import load_and_validate
    from odoo_synth.core.mask import apply_masking, leak_scan
    import psycopg

    rb = load_and_validate(RULES_DIR)
    assert rb.column_patterns, "rulebook must ship column patterns"

    with psycopg.connect(isolated_db, autocommit=True) as conn:
        with conn.cursor() as cur:
            originals = _seed_pattern(cur)

    summary = apply_masking(isolated_db, rb)
    # Pattern labels must have run.
    assert summary["pattern_labels"] > 0, "no pattern labels generated"
    assert summary["pattern_applied"] > 0, "pattern labels not applied"
    # Leak scan: the cache values must be gone.
    leaks = leak_scan(isolated_db, list(originals.values()))
    assert leaks == [], f"pattern backstop leaked: {leaks}"

    # Explicit-rule-wins: account_move.invoice_partner_display_name is covered
    # by an explicit rule, so it must not be counted in pattern_applied as a
    # pattern hit. We check the masked value is redacted (not the original) and
    # the column was handled (it's masked -- explicit rule applied).
    with psycopg.connect(isolated_db, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT invoice_partner_display_name, invoice_source_email "
                        "FROM account_move WHERE id=1")
            am_name, am_email = cur.fetchone()
            assert am_name != "Real Company Inc", "explicit rule didn't mask"
            assert "@company-real.com" not in (am_email or ""), "email leaked"
            # x_random_doc cache columns must be redacted, not the originals.
            cur.execute("SELECT some_display_name, complete_name FROM x_random_doc")
            rows = cur.fetchall()
            assert all(r[0] != "Real Company Inc" for r in rows), "pattern missed cache"
            assert all(r[1] != "Real Company Inc - child" for r in rows), "pattern missed complete_name"


# ---------------------------------------------------------------------------
# Module-metadata org-name leaks (55_module_metadata.yml)
# ---------------------------------------------------------------------------

_MODULE_SEED_TABLES = [
    "res_partner", "res_company", "ir_module_module", "ir_ui_view",
]


def _seed_module_metadata(cur):
    """Seed the two org-name leak vectors 55_module_metadata.yml closes:
    ir_module_module.author (custom module author = org name) and
    ir_ui_view.arch_db (custom report template hardcoding the org name)."""
    for tbl in _MODULE_SEED_TABLES:
        cur.execute(f'DROP TABLE IF EXISTS "{tbl}" CASCADE')
    cur.execute("""
        CREATE TABLE res_company (
            id serial PRIMARY KEY, name text, email text, phone text,
            street text, city text, zip text, vat text, company_registry text,
            website text, logo bytea
        )
    """)
    # Real org name in res_company (the source the arch replace reads from).
    cur.execute("""INSERT INTO res_company (name) VALUES
        ('Isha Life Pvt Ltd'),
        ('Isha Life Pvt Ltd - Tamil Nadu')""")
    cur.execute("""
        CREATE TABLE ir_module_module (
            id serial PRIMARY KEY, name text, author text, state text
        )
    """)
    # Custom module author = the org name; OCA module = third-party author.
    cur.execute("""INSERT INTO ir_module_module (name, author, state) VALUES
        ('isha_darkstore', 'Isha Life Pvt Ltd', 'installed'),
        ('some_oca_module', 'Odoo S.A.', 'installed')""")
    cur.execute("""
        CREATE TABLE ir_ui_view (
            id serial PRIMARY KEY, key text, name text, arch_db jsonb,
            arch_prev text
        )
    """)
    # Custom report template hardcodes the org name as a footer literal.
    # The SAME view's arch_prev (Odoo's view-edit history) carries the same
    # literal in its archived prior revision -- the 55_module_metadata.yml
    # global-drop rule must null arch_prev for ALL rows.
    cur.execute("""INSERT INTO ir_ui_view (key, name, arch_db, arch_prev) VALUES
        ('isha_darkstore.report_invoice_ds_packing_slip', 'report_invoice_ds_packing_slip',
         '{"en_US": "<t><small>Dark Store Fulfillment - Isha Life Pvt Ltd</small></t>"}'::jsonb,
         '<t><small>Dark Store Fulfillment - Isha Life Pvt Ltd (prev revision)</small></t>'),
        ('web.some_other_view', 'some_other_view',
         '{"en_US": "<t>do not touch me Some Other Literal</t>"}'::jsonb,
         '<t>some unrelated prior revision</t>')""")
    return {
        "org_name": "Isha Life Pvt Ltd",
        "org_branch": "Isha Life Pvt Ltd - Tamil Nadu",
        "oca_author": "Odoo S.A.",
        "packing_slip_view_key": "isha_darkstore.report_invoice_ds_packing_slip",
        "untouched_view_key": "web.some_other_view",
    }


def test_module_metadata_rules_close_org_name_leaks(isolated_db):
    """55_module_metadata.yml must (1) null ir_module_module.author globally
    and (2) replace the org name in ONLY the listed view's arch_db with the
    masked company name, leaving every other view verbatim."""
    from odoo_synth.core.rulebook import load_and_validate
    from odoo_synth.core.mask import apply_masking, leak_scan
    import psycopg

    rb = load_and_validate(RULES_DIR)
    with psycopg.connect(isolated_db, autocommit=True) as conn:
        with conn.cursor() as cur:
            originals = _seed_module_metadata(cur)

    summary = apply_masking(isolated_db, rb)
    # The scoped arch replace pass must have run and touched the listed view.
    assert summary["scoped_arch_replace"]["views_touched"] == 1, (
        f"expected 1 view touched, got {summary['scoped_arch_replace']}")
    assert summary["scoped_arch_replace"]["rows_updated"] == 1, (
        f"expected 1 row updated, got {summary['scoped_arch_replace']}")

    with psycopg.connect(isolated_db, autocommit=True) as conn:
        with conn.cursor() as cur:
            # 1. ir_module_module.author nulled globally (all rows, all modules).
            cur.execute("SELECT author FROM ir_module_module ORDER BY id")
            authors = [r[0] for r in cur.fetchall()]
            assert all(a is None for a in authors), (
                f"ir_module_module.author not nulled globally: {authors}")

            # 1b. ir_ui_view.arch_prev nulled globally -- Odoo's view-edit
            #     history column carried the org name in the packing-slip
            #     view's prior revision (darkstore leak-scan finding). Same
            #     rule class as ir_module_module.author: blanket drop, all rows.
            cur.execute("SELECT arch_prev FROM ir_ui_view ORDER BY id")
            arch_prevs = [r[0] for r in cur.fetchall()]
            assert all(a is None for a in arch_prevs), (
                f"ir_ui_view.arch_prev not nulled globally: {arch_prevs}")

            # 2. The listed view's arch_db no longer contains the real org name,
            #    but DOES contain the masked company name (proving replace, not
            #    redact). Read the masked company name to compare.
            cur.execute("SELECT name FROM res_company WHERE id=1")
            masked_name = cur.fetchone()[0]
            assert masked_name != originals["org_name"], "res.company.name not masked"
            cur.execute("SELECT arch_db->>'en_US' FROM ir_ui_view "
                        "WHERE key = %s", (originals["packing_slip_view_key"],))
            arch = cur.fetchone()[0]
            assert originals["org_name"] not in arch, (
                f"packing-slip arch still leaks org name: {arch}")
            assert masked_name in arch, (
                f"packing-slip arch should carry masked name {masked_name!r}: {arch}")

            # 3. The OTHER view is left verbatim -- NOT touched by the scoped pass.
            cur.execute("SELECT arch_db->>'en_US' FROM ir_ui_view "
                        "WHERE key = %s", (originals["untouched_view_key"],))
            other_arch = cur.fetchone()[0]
            assert "Some Other Literal" in other_arch, (
                "scoped pass touched a view it should have left alone "
                f"(only listed views may be rewritten): {other_arch}")

    # Leak scan against the full set of original org-name values: must be clean.
    leaks = leak_scan(isolated_db, [
        originals["org_name"], originals["org_branch"], originals["oca_author"],
    ])
    # The OCA author 'Odoo S.A.' is a third-party string that also gets nulled
    # by the global author drop, so it must be gone too. The other view still
    # contains the org name (intentionally, per rule 2's scoping) -- but that
    # view is NOT in our rulebook leak vectors here; in a real instance that
    # other view would be a separate flagged item. For THIS test we assert the
    # rulebook's declared vectors are clean.
    assert originals["org_name"] not in leaks, f"org name still leaked: {leaks}"
    assert originals["org_branch"] not in leaks, f"org branch still leaked: {leaks}"
    assert originals["oca_author"] not in leaks, f"oca author still leaked: {leaks}"
