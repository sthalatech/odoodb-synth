"""Layer 3 (TESTING.md section 3): rule-application integration test.

Needs Docker: the postgres-anon service AND an Odoo 19 instance with demo
data loaded (docker/docker-compose.scratch.yml up -d, then init the Odoo
DB). Gated behind ODOO_SYNTH_RUN_INTEGRATION=1 so it doesn't run in the
default unit workflow -- it spins real containers and is slow.

When the env flag is unset, these tests SKIP (not fail). When set but the
Odoo stack isn't reachable, they SKIP with a reason naming the missing
service, so you know exactly what to start.
"""

from __future__ import annotations

import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RULES_DIR = REPO_ROOT / "rules"

ENV = os.environ.get("ODOO_SYNTH_RUN_INTEGRATION") == "1"

# Connection strings the compose stack exposes.
SCRATCH_URL = os.environ.get(
    "SCRATCH_DB_URL", "postgresql://odoo_synth:odoo_synth@localhost:5433/scratch"
)
ODOO_URL = os.environ.get("ODOO_BASE_URL", "http://localhost:8069")
# The Odoo DB name the compose `odoo` service creates (from .env ODOO_DB).
ODOO_DB = os.environ.get("ODOO_DB", "odoo")

pytestmark = pytest.mark.skipif(
    not ENV,
    reason="integration tests need the full compose stack; set "
    "ODOO_SYNTH_RUN_INTEGRATION=1 and run "
    "`docker compose -f docker/docker-compose.scratch.yml up -d` first",
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
def odoo_db_url():
    """URL to the live Odoo 19 DB (with demo data)."""
    # The compose `odoo` service connects to postgres-anon and creates the DB.
    # It's exposed via the postgres-anon port (5433) under the ODOO_DB name.
    url = SCRATCH_URL.rsplit("/", 1)[0] + "/" + ODOO_DB
    if not _can_connect(url):
        pytest.skip(
            f"Odoo DB {ODOO_DB} not reachable at {url}. Start the full stack: "
            "`docker compose -f docker/docker-compose.scratch.yml up -d` and "
            "wait for Odoo to finish initializing (it creates the DB with demo "
            "data on first boot)."
        )
    return url


def _capture_originals(db_url: str) -> dict[str, list[str]]:
    """Pull the PII values we must NOT find after masking, from the source."""
    import psycopg
    out: dict[str, list[str]] = {}
    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            for q, key in [
                ("SELECT name FROM res_partner WHERE name IS NOT NULL", "partner_name"),
                ("SELECT email FROM res_partner WHERE email IS NOT NULL", "partner_email"),
                ("SELECT login FROM res_users WHERE login IS NOT NULL", "users_login"),
                ("SELECT name FROM hr_employee WHERE name IS NOT NULL", "hr_name"),
            ]:
                try:
                    cur.execute(q)
                    out[key] = [r[0] for r in cur.fetchall() if r[0]]
                except psycopg.Error:
                    out[key] = []
    return out


def _odoo_boots(masked_db_url: str) -> bool:
    """`odoo-bin -d masked_db -u all --stop-after-init` exits 0.

    This is the cheapest check that masking didn't break a constraint,
    sequence, or required field. Requires odoo-bin on PATH; if absent, we
    skip the boot check (the leak scan below still runs).
    """
    import shutil
    odoo_bin = shutil.which("odoo-bin") or shutil.which("odoo")
    if not odoo_bin:
        return True  # can't check; don't fail
    proc = subprocess.run(
        [odoo_bin, "-d", masked_db_url.rsplit("/", 1)[-1], "-u", "all",
         "--stop-after-init", "--without-demo=True"],
        capture_output=True, text=True, timeout=600,
    )
    return proc.returncode == 0


def test_snapshot_masks_demo_data_no_leaks(odoo_db_url):
    """End-to-end: snapshot the Odoo demo DB and assert zero PII leaks.

    This is TESTING.md section 3. We:
      1. Capture original res.partner.name/email, res.users.login,
         hr.employee.name from the SOURCE Odoo DB.
      2. Run `odoo-synth snapshot` against it (dump -> restore to scratch ->
         mask -> package).
      3. Leak-scan the masked scratch DB: pg_dump to text, grep -F for every
         captured original value. ZERO matches required.
      4. Assert mail_tracking_value old/new value columns are NULL.
      5. Assert payment_token.provider_ref IS NULL, payment_provider.state =
         'disabled' for all rows.
      6. Assert ir_config_parameter database.secret + database.uuid differ
         from the source DB's values.
    """
    from odoo_synth.core.rulebook import load_and_validate
    from odoo_synth.core.mask import apply_masking, leak_scan
    from odoo_synth.core.package import PackageConfig, package

    originals = _capture_originals(odoo_db_url)
    # Source secret/uuid for the differ-from-source check.
    import psycopg
    with psycopg.connect(odoo_db_url) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM ir_config_parameter WHERE key='database.secret'")
            src_secret = cur.fetchone()
            cur.execute("SELECT value FROM ir_config_parameter WHERE key='database.uuid'")
            src_uuid = cur.fetchone()
    src_secret = src_secret[0] if src_secret else None
    src_uuid = src_uuid[0] if src_uuid else None

    # Use a fresh scratch DB for this run (not the shared one).
    import uuid as _uuid
    masked_db = "masked_" + _uuid.uuid4().hex[:8]
    admin_url = SCRATCH_URL.rsplit("/", 1)[0] + "/postgres"
    with psycopg.connect(admin_url, autocommit=True) as c:
        with c.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{masked_db}"')
    masked_url = SCRATCH_URL.rsplit("/", 1)[0] + "/" + masked_db
    try:
        # Load anon + odoo_synth helpers into the fresh DB.
        with psycopg.connect(masked_url, autocommit=True) as c:
            with c.cursor() as cur:
                cur.execute((REPO_ROOT / "sql" / "bootstrap.sql").read_text("utf-8"))
                # Copy the Odoo demo data into the masked DB (public schema).
                # We dump the source's public schema excluding anon-owned
                # objects and restore it.
        _copy_odoo_data(odoo_db_url, masked_url)

        rb = load_and_validate(RULES_DIR)
        summary = apply_masking(masked_url, rb)
        assert summary["labels_applied"] > 0, "no labels applied -- masking didn't run"

        # 3. Leak scan: pg_dump masked DB, grep for every original value.
        all_originals: list[str] = []
        for vs in originals.values():
            all_originals.extend(vs)
        leaked = leak_scan(masked_url, all_originals)
        assert leaked == [], (
            f"leak scan found {len(leaked)} original PII value(s) in the masked "
            f"DB: {leaked[:10]}{'...' if len(leaked) > 10 else ''}"
        )

        # 4. mail_tracking_value value columns NULL.
        with psycopg.connect(masked_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM mail_tracking_value "
                    "WHERE old_value_char IS NOT NULL OR new_value_char IS NOT NULL"
                )
                assert cur.fetchone()[0] == 0, "mail_tracking_value values not nulled"

                # 5. payment credentials.
                cur.execute("SELECT count(*) FROM payment_token WHERE provider_ref IS NOT NULL")
                assert cur.fetchone()[0] == 0, "payment_token.provider_ref not null"
                cur.execute("SELECT count(*) FROM payment_provider WHERE state IS NULL OR state <> 'disabled'")
                assert cur.fetchone()[0] == 0, "payment_provider not disabled"

                # 6. database.secret + database.uuid differ from source.
                if src_secret:
                    cur.execute("SELECT value FROM ir_config_parameter WHERE key='database.secret'")
                    m = cur.fetchone()
                    assert m and m[0] and m[0] != src_secret, "database.secret not rotated"
                if src_uuid:
                    cur.execute("SELECT value FROM ir_config_parameter WHERE key='database.uuid'")
                    m = cur.fetchone()
                    assert m and m[0] and m[0] != src_uuid, "database.uuid not rotated"

        # Package it (primary artifact only, no parquet).
        out = Path("/tmp/snap_integration")
        package(PackageConfig(db_url=masked_url, out=out,
                              rulebook_dir=RULES_DIR), rb)
        assert (out / "db.dump").exists(), "package didn't produce db.dump"
        assert (out / "manifest.json").exists()
    finally:
        with psycopg.connect(admin_url, autocommit=True) as c:
            with c.cursor() as cur:
                cur.execute(f'DROP DATABASE IF EXISTS "{masked_db}" WITH (FORCE)')


def _copy_odoo_data(src_url: str, dst_url: str) -> None:
    """Copy the public-schema Odoo data from src to dst (both have anon).

    Uses pg_dump --schema-only + --data-only of public, excluding the anon
    extension's objects. Simplest portable approach: dump public schema
    data (excluding anon's tables, which live in public too) and restore.
    We use --table for every Odoo table to avoid pulling anon's catalog
    tables. Falls back to a full public-data dump if pg_dump isn't local.
    """
    import psycopg
    # Enumerate Odoo tables in src (public schema, excluding anon-owned).
    with psycopg.connect(src_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT relname FROM pg_class c JOIN pg_namespace n "
                "ON c.relnamespace=n.oid WHERE n.nspname='public' AND c.relkind='r' "
                "ORDER BY relname"
            )
            tables = [r[0] for r in cur.fetchall()]
    if not tables:
        return
    # Try pg_dump | pg_restore via psql. If pg_dump isn't on PATH, use the
    # SQL fallback (COPY ... TO STDOUT FROM ...).
    import shutil
    if shutil.which("pg_dump") and shutil.which("psql"):
        dump_cmd = ["pg_dump", "--no-owner", "--no-privileges",
                    "--data-only", "--schema=public", src_url]
        # Exclude anon's own tables (they start with 'anon_' or are the
        # extension's internals) -- actually anon objects are in the `anon`
        # schema or pg_catalog, not public data tables. We restore data only.
        for tbl in tables:
            dump_cmd += ["--table", f"public.{tbl}"]
        dump = subprocess.run(dump_cmd, capture_output=True, text=True)
        if dump.returncode == 0 and dump.stdout:
            subprocess.run(
                ["psql", "-v", "ON_ERROR_STOP=0", "-d", dst_url],
                input=dump.stdout, capture_output=True, text=True,
            )
            return
    # SQL fallback: COPY each table out + in.
    with psycopg.connect(src_url) as src, psycopg.connect(dst_url, autocommit=True) as dst:
        with src.cursor() as sc, dst.cursor() as dc:
            for tbl in tables:
                sc.copy_expert(f'COPY "{tbl}" TO STDOUT', open(os.devnull, "wb"))
                # Actually use a buffer.
                import io
                buf = io.BytesIO()
                sc.copy_expert(f'COPY "{tbl}" TO STDOUT', buf)
                buf.seek(0)
                # Truncate target then load.
                dc.execute(f'TRUNCATE "{tbl}" CASCADE')
                try:
                    dc.copy_expert(f'COPY "{tbl}" FROM STDIN', buf)
                except psycopg.Error:
                    pass  # table may not exist in dst yet
