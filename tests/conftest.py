"""Shared pytest fixtures.

SQL function unit tests (tests/unit/test_sql_functions.py) need a
Postgres with the `anon` extension and the odoo_synth bootstrap functions
loaded -- i.e. the `postgres-anon` service from
docker/docker-compose.scratch.yml. They are skipped automatically if
SCRATCH_DB_URL is unset or the database is unreachable, so `pytest
tests/unit/test_rulebook_validation.py` (which needs no DB) still runs
in any environment.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
RULES_DIR = REPO_ROOT / "rules"
BOOTSTRAP_SQL = REPO_ROOT / "sql" / "bootstrap.sql"


def _can_connect(db_url: str) -> bool:
    try:
        import psycopg
    except Exception:
        return False
    try:
        with psycopg.connect(db_url, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def scratch_db_url() -> str:
    url = os.environ.get("SCRATCH_DB_URL")
    if not url:
        pytest.skip("SCRATCH_DB_URL unset; start the scratch Postgres with "
                    "`docker compose -f docker/docker-compose.scratch.yml up -d postgres-anon`")
    if not _can_connect(url):
        pytest.skip(f"cannot connect to SCRATCH_DB_URL={url}; is the "
                    "postgres-anon container running?")
    return url


@pytest.fixture(scope="session")
def db_conn(scratch_db_url):
    """A live connection to the scratch DB. The compose init already loaded
    bootstrap.sql; we re-apply it idempotently so tests also work against a
    bare anon-enabled Postgres that didn't go through the init script."""
    import psycopg
    with psycopg.connect(scratch_db_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(BOOTSTRAP_SQL.read_text(encoding="utf-8"))
        yield conn


@pytest.fixture
def cur(db_conn):
    with db_conn.cursor() as c:
        yield c
