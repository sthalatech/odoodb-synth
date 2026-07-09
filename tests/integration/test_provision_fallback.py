"""Provision pg_restore-fallback path regression test (postgres-anon only).

Catches the bug where provision's pg_restore fallback restored the bundle
into the ADMIN/maintenance DB (cfg.db_url, which points at `postgres` for
CREATE DATABASE) instead of the freshly-created TARGET DB. The first real-
schema provisioning run on darkstore hit this: restore reported rc=0,
credential-verify passed VACUOUSLY (every Odoo table absent -> missing_ok
returned None -> check passes), but masked_test had zero tables while the
maintenance `postgres` DB got 526 Odoo tables dumped into it.

This test needs only the postgres-anon container (no Odoo), so it runs in
the normal CI layer-2 job alongside test_mask.py / test_scan_cli.py.
"""
from __future__ import annotations

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

# provision's pg_restore fallback honors ODOO_SYNTH_PG_RESTORE. The host
# pg_restore may be an older major than the PG18 server in the container
# (version mismatch), so route restore through the container binary. The
# in-container DB URL differs from the host URL (localhost:5433), so we also
# set ODOO_SYNTH_RESTORE_DB_URL to the in-container socket form. These mirror
# the pattern core/package.py + mask.py use for the same reason.
# Route pg_restore through the container binary (host pg_restore may be
# older than the PG18 server -- "unsupported version in file header"). The
# {dbname} placeholder lets the code substitute the dynamic target DB name,
# so the in-container socket URL points at the right DB. We use the unix
# socket (TCP on localhost:5433 is refused inside the container's netns).
if not os.environ.get("ODOO_SYNTH_PG_RESTORE"):
    os.environ["ODOO_SYNTH_PG_RESTORE"] = (
        "docker exec -i odoo-synth-postgres-anon pg_restore -U odoo_synth"
    )
if not os.environ.get("ODOO_SYNTH_RESTORE_DB_URL"):
    os.environ["ODOO_SYNTH_RESTORE_DB_URL"] = (
        "postgresql://odoo_synth:odoo_synth@%2Fvar%2Frun%2Fpostgresql/{dbname}"
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
    if not url:
        pytest.skip("SCRATCH_DB_URL unset; start postgres-anon")
    if not _can_connect(url):
        pytest.skip(f"cannot connect to SCRATCH_DB_URL={url}")
    return url


def _make_bundle(admin_url: str, bundle_dir: Path) -> Path:
    """Build a minimal masked bundle (db.dump + manifest.json) from a tiny
    seeded schema that looks Odoo-shaped enough for provision + verify."""
    import psycopg
    import json

    src = "provtb_" + uuid.uuid4().hex[:10]
    with psycopg.connect(admin_url, autocommit=True) as c, c.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{src}"')
    src_url = admin_url.rsplit("/", 1)[0] + "/" + src
    try:
        with psycopg.connect(src_url, autocommit=True) as c, c.cursor() as cur:
            # Odoo-shaped: res_partner + ir_config_parameter (the two tables
            # provision's restore-verify looks for) + payment_provider (verify).
            cur.execute("CREATE TABLE res_partner (id serial PRIMARY KEY, name text, email text)")
            cur.execute("INSERT INTO res_partner VALUES (1,'Fake Person','fake@example.invalid')")
            cur.execute("CREATE TABLE ir_config_parameter (id serial PRIMARY KEY, key text, value text)")
            cur.execute("INSERT INTO ir_config_parameter VALUES "
                        "(1,'database.secret','rotatedsecret64hex'),(2,'database.uuid','rotated-uuid'),"
                        "(3,'mail.force.smtp.from','x')")
            cur.execute("CREATE TABLE payment_provider (id serial PRIMARY KEY, state text)")
            cur.execute("INSERT INTO payment_provider VALUES (1,'disabled')")
            cur.execute("CREATE TABLE payment_token (id serial PRIMARY KEY, provider_ref text)")
            cur.execute("CREATE TABLE mail_tracking_value (id serial PRIMARY KEY, old_value_char text, new_value_char text)")
        bundle_dir.mkdir(parents=True, exist_ok=True)
        dump = bundle_dir / "db.dump"
        # Run pg_dump INSIDE the postgres-anon container (host pg_dump may be
        # an older major than the PG18 server -- version mismatch). The
        # container connects over its internal localhost.
        cdbname = src
        proc = subprocess.run(
            ["docker", "exec", "odoo-synth-postgres-anon",
             "pg_dump", "-Fc", "--no-owner", "--no-privileges",
             "-U", "odoo_synth", "-d", cdbname],
            stdout=open(dump, "wb"), stderr=subprocess.PIPE,
        )
        assert proc.returncode == 0, f"pg_dump failed: {proc.stderr.decode()}"
        (bundle_dir / "manifest.json").write_text(
            json.dumps({"source": "test", "primary_artifact": "db.dump"}), "utf-8")
    finally:
        with psycopg.connect(admin_url, autocommit=True) as c, c.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{src}" WITH (FORCE)')
    return bundle_dir


def test_provision_pg_restore_fallback_loads_target_db_not_admin(admin_url, tmp_path):
    """The pg_restore fallback must restore into the TARGET DB, not the
    maintenance DB. Regression for the darkstore provisioning bug."""
    import psycopg
    from odoo_synth.core import provision
    from odoo_synth.core.rulebook import load_and_validate

    bundle = _make_bundle(admin_url, tmp_path / "bundle")
    target = "provtarget_" + uuid.uuid4().hex[:10]
    # admin_url points at `scratch`; use it as the admin URL (ends in /scratch).
    cfg = provision.ProvisionConfig(
        bundle=bundle, db_name=target,
        db_url=admin_url,  # admin URL -- CREATE DATABASE works against this
        launch=False,
    )
    try:
        report = provision.provision(cfg, load_and_validate(RULES_DIR))
        assert report["restored"] is True
        assert report["neutralized"] is True
        assert report["credential_verification"]["passed"] is True
        # The key regression assertion: the TARGET DB has the tables.
        target_url = admin_url.rsplit("/", 1)[0] + "/" + target
        with psycopg.connect(target_url, autocommit=True) as c, c.cursor() as cur:
            cur.execute("SELECT to_regclass('public.res_partner')")
            assert cur.fetchone()[0] == "res_partner", \
                "restore landed in the wrong DB (target empty)"
            cur.execute("SELECT count(*) FROM res_partner")
            assert cur.fetchone()[0] == 1
            # neutralize ran on the TARGET (payment_provider.state=disabled)
            cur.execute("SELECT count(*) FROM payment_provider WHERE state<>'disabled'")
            assert cur.fetchone()[0] == 0
        # And the ADMIN/maintenance DB did NOT get Odoo tables dumped into it.
        with psycopg.connect(admin_url, autocommit=True) as c, c.cursor() as cur:
            cur.execute("SELECT to_regclass('public.res_partner')")
            assert cur.fetchone()[0] is None, \
                "restore leaked Odoo tables into the maintenance DB"
    finally:
        with psycopg.connect(admin_url, autocommit=True) as c, c.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{target}" WITH (FORCE)')


def test_provision_verify_fails_on_empty_target_db(admin_url, tmp_path):
    """If the restore loaded nothing into the target, credential-verify must
    FAIL (not pass vacuously). Guards the missing_ok=True silent-pass path."""
    import psycopg
    from odoo_synth.core import provision
    from odoo_synth.core.rulebook import load_and_validate

    # A bundle whose db.dump has NO Odoo tables (so restore loads nothing).
    bundle = tmp_path / "empty_bundle"
    bundle.mkdir()
    src = "emptyprov_" + uuid.uuid4().hex[:10]
    with psycopg.connect(admin_url, autocommit=True) as c, c.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{src}"')
    src_url = admin_url.rsplit("/", 1)[0] + "/" + src
    try:
        with psycopg.connect(src_url, autocommit=True) as c, c.cursor() as cur:
            cur.execute("CREATE TABLE not_odoo (id int)")
        subprocess.run(["docker", "exec", "odoo-synth-postgres-anon",
                        "pg_dump", "-Fc", "--no-owner", "--no-privileges",
                        "-U", "odoo_synth", "-d", src],
                       stdout=open(bundle / "db.dump", "wb"), check=True)
        import json
        (bundle / "manifest.json").write_text("{}", "utf-8")
    finally:
        with psycopg.connect(admin_url, autocommit=True) as c, c.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{src}" WITH (FORCE)')

    target = "emptytarget_" + uuid.uuid4().hex[:10]
    cfg = provision.ProvisionConfig(
        bundle=bundle, db_name=target, db_url=admin_url, launch=False,
    )
    try:
        with pytest.raises(provision.ProvisionError):
            provision.provision(cfg, load_and_validate(RULES_DIR))
    finally:
        with psycopg.connect(admin_url, autocommit=True) as c, c.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{target}" WITH (FORCE)')
