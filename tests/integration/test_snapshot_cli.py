"""End-to-end CLI test: `odoo-synth snapshot` + `odoo-synth up` on a seeded
Odoo-shaped source DB. Exercises the real CLI binary path (dump -> restore
to scratch -> mask -> package -> provision), not just the library calls.

Needs the postgres-anon container (not the full Odoo stack), so it runs in
the default CI suite alongside tests/integration/test_mask.py. Skips if
SCRATCH_DB_URL is unset or unreachable.

Uses ODOO_SYNTH_PG_DUMP/PG_RESTORE container overrides + the in-container
URL envs so the host's older pg_dump doesn't block the pipeline (the host
typically ships PG16 client, the container is PG18 -- a PG16 pg_dump can't
dump a PG18 server).
"""

from __future__ import annotations

import json
import os
import subprocess
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
    url = os.environ.get("SCRATCH_DB_URL")
    if not url or not _can_connect(url):
        pytest.skip("SCRATCH_DB_URL unset or unreachable; start postgres-anon")
    return url


def _container_name() -> str:
    """The postgres-anon container name, for the docker-exec pg_dump override."""
    return os.environ.get("ODOO_SYNTH_CONTAINER", "odoo-synth-postgres-anon")


def _seed_source(cur):
    """Create Odoo-shaped tables + seed known PII in a source DB."""
    for tbl in ("res_partner", "ir_config_parameter", "payment_token",
                "payment_provider", "mail_tracking_value", "hr_employee",
                "ir_attachment"):
        cur.execute(f'DROP TABLE IF EXISTS "{tbl}" CASCADE')
    cur.execute("""CREATE TABLE res_partner (
        id serial PRIMARY KEY, name text, email text, vat text, comment text)""")
    cur.execute("""INSERT INTO res_partner (name,email,vat,comment) VALUES
        ('Alice Acme','alice@real.com','BE0123456789','private note')""")
    cur.execute("""CREATE TABLE ir_config_parameter (
        id serial PRIMARY KEY, key text, value text)""")
    cur.execute("""INSERT INTO ir_config_parameter (key,value) VALUES
        ('database.secret','SRC_SECRET_123'),
        ('database.uuid','11111111-1111-1111-1111-111111111111'),
        ('mail.catchall.domain','example.com')""")
    cur.execute("""CREATE TABLE payment_token (
        id serial PRIMARY KEY, provider_ref text, active boolean)""")
    cur.execute("INSERT INTO payment_token VALUES (1,'live_token_xyz',true)")
    cur.execute("""CREATE TABLE payment_provider (
        id serial PRIMARY KEY, state text)""")
    cur.execute("INSERT INTO payment_provider VALUES (1,'enabled')")
    cur.execute("""CREATE TABLE mail_tracking_value (
        id serial PRIMARY KEY, old_value_char text, new_value_char text)""")
    cur.execute("INSERT INTO mail_tracking_value VALUES (1,'alice@real.com','alice2@real.com')")
    cur.execute("""CREATE TABLE hr_employee (
        id serial PRIMARY KEY, name text, birthday date, pin text)""")
    cur.execute("INSERT INTO hr_employee VALUES (1,'Bob Builder','1985-06-15','9999')")
    cur.execute("""CREATE TABLE ir_attachment (
        id serial PRIMARY KEY, name text, datas bytea, index_content text, res_model text)""")
    cur.execute("""INSERT INTO ir_attachment VALUES
        (1,'John_Smith_Resume.pdf',decode('cafebabe','hex'),'extracted text','res.partner')""")


def test_cli_snapshot_then_up_masks_and_provisions(admin_url):
    """Full CLI round-trip: snapshot a source DB, then up the masked bundle.

    Asserts the provisioned DB has masked values + rotated secrets, and the
    credential-verification check passed -- the same gates as TESTING.md
    section 3/4, driven through the actual `odoo-synth` binary.
    """
    import psycopg
    suffix = uuid.uuid4().hex[:8]
    src_db = "clisrc_" + suffix
    mask_db = "climask_" + suffix
    prov_db = "cliprov_" + suffix
    base = admin_url.rsplit("/", 1)[0]

    def _create(name):
        with psycopg.connect(base + "/postgres", autocommit=True) as c:
            with c.cursor() as cur:
                cur.execute(f'CREATE DATABASE "{name}"')

    def _drop(name):
        with psycopg.connect(base + "/postgres", autocommit=True) as c:
            with c.cursor() as cur:
                cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')

    try:
        # Source DB with seeded PII.
        _create(src_db)
        with psycopg.connect(base + "/" + src_db, autocommit=True) as c:
            with c.cursor() as cur:
                _seed_source(cur)
        # Mask target DB with anon + bootstrap loaded.
        _create(mask_db)
        with psycopg.connect(base + "/" + mask_db, autocommit=True) as c:
            with c.cursor() as cur:
                cur.execute((REPO_ROOT / "sql" / "bootstrap.sql").read_text("utf-8"))

        out = Path("/tmp/cli_snap_test")
        if out.exists():
            subprocess.run(["rm", "-rf", str(out)], check=True)
        container = _container_name()
        env = {
            **os.environ,
            "ODOO_SYNTH_PG_DUMP": f"docker exec {container} pg_dump",
            "ODOO_SYNTH_PG_RESTORE": f"docker exec -i {container} pg_restore",
            # In-container URLs (port 5432 inside the container, not the host's 5433).
            "ODOO_SYNTH_DUMP_DB_URL": f"postgresql://odoo_synth:odoo_synth@localhost:5432/{src_db}",
            "ODOO_SYNTH_RESTORE_DB_URL": f"postgresql://odoo_synth:odoo_synth@localhost:5432/{mask_db}",
            "ODOO_SYNTH_PACKAGE_DB_URL": f"postgresql://odoo_synth:odoo_synth@localhost:5432/{mask_db}",
        }
        # 1. snapshot
        r = subprocess.run(
            ["odoo-synth", "snapshot", "--db", src_db, "--rules", str(RULES_DIR),
             "--out", str(out), "--source-db-url", base + "/" + src_db,
             "--scratch-db-url", base + "/" + mask_db],
            capture_output=True, text=True, env=env,
        )
        assert r.returncode == 0, f"snapshot failed: {r.stderr}\n{r.stdout}"
        assert (out / "db.dump").exists(), "snapshot didn't produce db.dump"
        mf = json.loads((out / "manifest.json").read_text("utf-8"))
        assert mf["row_counts"]["res_partner"] == 1
        assert mf["rulebook_strategies"] == 19

        # 2. up (provision) into a fresh DB
        _create(prov_db)  # provision recreates it, but it must not exist for the
        # recreate's DROP IF EXISTS to be clean -- actually provision drops+creates,
        # so create first then let provision DROP+CREATE.
        env_prov = {**env,
                    "ODOO_SYNTH_RESTORE_DB_URL": f"postgresql://odoo_synth:odoo_synth@localhost:5432/{prov_db}"}
        # Drop it so provision's own recreate is the creator (matches real use).
        _drop(prov_db)
        r2 = subprocess.run(
            ["odoo-synth", "up", "--from", str(out), "--db", prov_db,
             "--db-url", base + "/" + prov_db, "--no-launch"],
            capture_output=True, text=True, env=env_prov,
        )
        assert r2.returncode == 0, f"up failed: {r2.stderr}\n{r2.stdout}"
        assert "creds_verified=True" in r2.stdout, f"creds not verified: {r2.stdout}"

        # 3. Assert the provisioned DB has MASKED data + rotated secrets.
        with psycopg.connect(base + "/" + prov_db) as c:
            with c.cursor() as cur:
                cur.execute("SELECT name,email,vat,comment FROM res_partner WHERE id=1")
                p = cur.fetchone()
                assert p[0] != "Alice Acme", f"name not masked: {p[0]}"
                assert p[1] != "alice@real.com" and "@example.invalid" in p[1], f"email: {p[1]}"
                assert p[2] != "BE0123456789", "vat not masked"
                # payment_token.provider_ref dropped, active false
                cur.execute("SELECT provider_ref,active FROM payment_token WHERE id=1")
                tk = cur.fetchone()
                assert tk[0] is None, "provider_ref not dropped"
                assert tk[1] is False, "active not forced false"
                # payment_provider disabled
                cur.execute("SELECT state FROM payment_provider WHERE id=1")
                assert cur.fetchone()[0] == "disabled"
                # mail_tracking_value nulled
                cur.execute("SELECT count(*) FROM mail_tracking_value WHERE old_value_char IS NOT NULL OR new_value_char IS NOT NULL")
                assert cur.fetchone()[0] == 0
                # secrets rotated (different from source)
                cur.execute("SELECT value FROM ir_config_parameter WHERE key='database.secret'")
                s = cur.fetchone()[0]
                assert s and s != "SRC_SECRET_123", "secret not rotated"
                cur.execute("SELECT value FROM ir_config_parameter WHERE key='database.uuid'")
                u = cur.fetchone()[0]
                assert u and u != "11111111-1111-1111-1111-111111111111", "uuid not rotated"
                # attachment filename scrubbed
                cur.execute("SELECT name FROM ir_attachment WHERE id=1")
                an = cur.fetchone()[0]
                assert an != "John_Smith_Resume.pdf" and an.startswith("attachment_1"), f"filename not scrubbed: {an}"
                # non-secret config param kept
                cur.execute("SELECT value FROM ir_config_parameter WHERE key='mail.catchall.domain'")
                assert cur.fetchone()[0] == "example.com", "non-secret param wrongly nulled"
    finally:
        for n in (src_db, mask_db, prov_db):
            _drop(n)
        subprocess.run(["rm", "-rf", "/tmp/cli_snap_test"], capture_output=True)
