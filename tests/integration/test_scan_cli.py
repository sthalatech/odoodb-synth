"""Integration test: `odoo-synth snapshot` writes schema.json and
`odoo-synth rules scan` flags undeclared PII-shaped fields against the real
rulebook.

Needs the postgres-anon container only (not the full Odoo stack), so it
runs in the default CI suite. Skips if SCRATCH_DB_URL is unset/unreachable.
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
    return os.environ.get("ODOO_SYNTH_CONTAINER", "odoo-synth-postgres-anon")


def _seed_with_gap(cur):
    """Seed a source DB with: a covered model (res.partner), a partially
    covered model (crm.lead -- name + partner_id intentionally uncovered),
    and an undeclared model (x.custom.ticket)."""
    for tbl in ("res_partner", "crm_lead", "x_custom_ticket"):
        cur.execute(f'DROP TABLE IF EXISTS "{tbl}" CASCADE')
    cur.execute("""CREATE TABLE res_partner (
        id serial PRIMARY KEY, name text, email text, phone text,
        street text, vat text, comment text, image_1920 bytea,
        partner_latitude numeric)""")
    cur.execute("""INSERT INTO res_partner (name,email,phone,street,vat,comment)
        VALUES ('Alice Acme','alice@real.com','+32470123456','Rue de Real 1',
                'BE0123456789','private note')""")
    cur.execute("""CREATE TABLE crm_lead (
        id serial PRIMARY KEY, name text NOT NULL,
        partner_id integer REFERENCES res_partner(id),
        email_from text, phone text, description text)""")
    cur.execute("""INSERT INTO crm_lead (name,partner_id,email_from,description)
        VALUES ('Big Deal',1,'lead@real.com','customer address here')""")
    cur.execute("""CREATE TABLE x_custom_ticket (
        id serial PRIMARY KEY, subject text,
        customer_id integer REFERENCES res_partner(id),
        internal_note text, payload bytea)""")
    cur.execute("""INSERT INTO x_custom_ticket (subject,customer_id,internal_note)
        VALUES ('issue',1,'secret notes')""")


def test_snapshot_writes_schema_json_and_rules_scan_flags_gaps(admin_url):
    """snapshot -> schema.json; rules scan -> findings + non-zero exit."""
    import psycopg
    suffix = uuid.uuid4().hex[:8]
    src_db = "scansrc_" + suffix
    mask_db = "scanmask_" + suffix
    base = admin_url.rsplit("/", 1)[0]
    out = Path("/tmp/cli_scan_test")

    def _create(name):
        with psycopg.connect(base + "/postgres", autocommit=True) as c:
            with c.cursor() as cur:
                cur.execute(f'CREATE DATABASE "{name}"')

    def _drop(name):
        with psycopg.connect(base + "/postgres", autocommit=True) as c:
            with c.cursor() as cur:
                cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')

    try:
        _create(src_db)
        with psycopg.connect(base + "/" + src_db, autocommit=True) as c:
            with c.cursor() as cur:
                _seed_with_gap(cur)
        _create(mask_db)
        with psycopg.connect(base + "/" + mask_db, autocommit=True) as c:
            with c.cursor() as cur:
                cur.execute((REPO_ROOT / "sql" / "bootstrap.sql").read_text("utf-8"))

        if out.exists():
            subprocess.run(["rm", "-rf", str(out)], check=True)
        container = _container_name()
        env = {
            **os.environ,
            "ODOO_SYNTH_PG_DUMP": f"docker exec {container} pg_dump",
            "ODOO_SYNTH_PG_RESTORE": f"docker exec -i {container} pg_restore",
            "ODOO_SYNTH_DUMP_DB_URL": f"postgresql://odoo_synth:odoo_synth@localhost:5432/{src_db}",
            "ODOO_SYNTH_RESTORE_DB_URL": f"postgresql://odoo_synth:odoo_synth@localhost:5432/{mask_db}",
            "ODOO_SYNTH_PACKAGE_DB_URL": f"postgresql://odoo_synth:odoo_synth@localhost:5432/{mask_db}",
        }
        # 1. snapshot -> must emit schema.json
        r = subprocess.run(
            ["odoo-synth", "snapshot", "--db", src_db, "--rules", str(RULES_DIR),
             "--out", str(out), "--source-db-url", base + "/" + src_db,
             "--scratch-db-url", base + "/" + mask_db],
            capture_output=True, text=True, env=env,
        )
        assert r.returncode == 0, f"snapshot failed: {r.stderr}\n{r.stdout}"
        assert (out / "schema.json").exists(), "snapshot didn't write schema.json"
        snap = json.loads((out / "schema.json").read_text("utf-8"))
        assert snap["source"] == "pg_catalog"
        assert "x_custom_ticket" in snap["tables"]
        # FK target resolved from pg_constraint.
        assert snap["tables"]["crm_lead"]["partner_id"]["fk_target"] == "res_partner.id"

        # 2. rules scan -> flags the gaps, non-zero exit (CI gate).
        r = subprocess.run(
            ["odoo-synth", "rules", "scan", "--bundle", str(out),
             "--rules", str(RULES_DIR)],
            capture_output=True, text=True, env=env,
        )
        assert r.returncode != 0, "rules scan should exit non-zero on findings"
        combined = r.stdout + r.stderr
        # The undeclared model's columns are flagged.
        assert "x_custom_ticket.subject" in combined
        assert "x_custom_ticket.customer_id" in combined
        assert "x.custom.ticket" in combined  # undeclared-model label
        # The partially-covered model's gaps are flagged.
        assert "crm_lead.name" in combined
        assert "crm_lead.partner_id" in combined
        # Covered columns are NOT flagged.
        assert "res_partner.email" not in combined
        assert "crm_lead.email_from" not in combined

        # 3. rules diff runs the same gate.
        r2 = subprocess.run(
            ["odoo-synth", "rules", "diff", "--bundle", str(out),
             "--rules", str(RULES_DIR)],
            capture_output=True, text=True, env=env,
        )
        assert r2.returncode != 0, "rules diff should exit non-zero on findings"
    finally:
        _drop(src_db)
        _drop(mask_db)
        if out.exists():
            subprocess.run(["rm", "-rf", str(out)], check=False)


def test_rules_scan_passes_when_schema_is_fully_covered(admin_url):
    """A bundle whose PII columns are all covered -> rules scan exit 0."""
    import psycopg
    suffix = uuid.uuid4().hex[:8]
    src_db = "scanclean_" + suffix
    mask_db = "scancleanmask_" + suffix
    base = admin_url.rsplit("/", 1)[0]
    out = Path("/tmp/cli_scan_clean")

    def _create(name):
        with psycopg.connect(base + "/postgres", autocommit=True) as c:
            with c.cursor() as cur:
                cur.execute(f'CREATE DATABASE "{name}"')

    def _drop(name):
        with psycopg.connect(base + "/postgres", autocommit=True) as c:
            with c.cursor() as cur:
                cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')

    try:
        _create(src_db)
        with psycopg.connect(base + "/" + src_db, autocommit=True) as c:
            with c.cursor() as cur:
                cur.execute('DROP TABLE IF EXISTS res_partner CASCADE')
                # Only res.partner, every PII column covered by 10_core.yml.
                cur.execute("""CREATE TABLE res_partner (
                    id serial PRIMARY KEY, name text, email text, phone text,
                    street text, city text, zip text, vat text, comment text,
                    website text, image_1920 bytea, partner_latitude numeric)""")
                cur.execute("INSERT INTO res_partner (name,email) VALUES ('x','y')")
        _create(mask_db)
        with psycopg.connect(base + "/" + mask_db, autocommit=True) as c:
            with c.cursor() as cur:
                cur.execute((REPO_ROOT / "sql" / "bootstrap.sql").read_text("utf-8"))

        if out.exists():
            subprocess.run(["rm", "-rf", str(out)], check=True)
        container = _container_name()
        env = {
            **os.environ,
            "ODOO_SYNTH_PG_DUMP": f"docker exec {container} pg_dump",
            "ODOO_SYNTH_PG_RESTORE": f"docker exec -i {container} pg_restore",
            "ODOO_SYNTH_DUMP_DB_URL": f"postgresql://odoo_synth:odoo_synth@localhost:5432/{src_db}",
            "ODOO_SYNTH_RESTORE_DB_URL": f"postgresql://odoo_synth:odoo_synth@localhost:5432/{mask_db}",
            "ODOO_SYNTH_PACKAGE_DB_URL": f"postgresql://odoo_synth:odoo_synth@localhost:5432/{mask_db}",
        }
        r = subprocess.run(
            ["odoo-synth", "snapshot", "--db", src_db, "--rules", str(RULES_DIR),
             "--out", str(out), "--source-db-url", base + "/" + src_db,
             "--scratch-db-url", base + "/" + mask_db],
            capture_output=True, text=True, env=env,
        )
        assert r.returncode == 0, f"snapshot failed: {r.stderr}\n{r.stdout}"
        r = subprocess.run(
            ["odoo-synth", "rules", "scan", "--bundle", str(out),
             "--rules", str(RULES_DIR)],
            capture_output=True, text=True, env=env,
        )
        assert r.returncode == 0, (
            f"rules scan should pass on a covered schema; got:\n{r.stdout}\n{r.stderr}"
        )
        assert "OK" in (r.stdout + r.stderr)
    finally:
        _drop(src_db)
        _drop(mask_db)
        if out.exists():
            subprocess.run(["rm", "-rf", str(out)], check=False)
