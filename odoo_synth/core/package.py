"""Package the masked result into a restorable artifact.

Primary artifact: a pg_dump custom-format file of the masked scratch DB
(`<out>/db.dump`). This is what `odoo-synth up` restores.

Secondary artifact (optional, --with-parquet): per-table Parquet export via
DuckDB. DuckDB is behind the `[parquet]` optional extra so the core tool
doesn't require it -- if --with-parquet is passed but duckdb isn't
importable, package() fails with a clear message pointing at the extra,
rather than silently producing a partial artifact.

manifest.json records: schema hash (pg_dump --schema-only hash), per-table
row counts, the rulebook version/hash applied, and a timestamp -- enough
for a consumer to verify what they're restoring and for `rules diff` to
detect rulebook drift.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .rulebook import Rulebook


class PackageError(Exception):
    """Raised when packaging fails."""


@dataclass
class PackageConfig:
    db_url: str  # masked scratch DB URL
    out: Path
    with_parquet: bool = False
    # rulebook hash for the manifest (caller computes; we just record it).
    rulebook_dir: Path | None = None


def package(cfg: PackageConfig, rulebook: Rulebook | None = None) -> Path:
    """Produce the output artifact from a masked scratch DB.

    Writes:
      <out>/db.dump             # pg_dump -Fc custom format (primary)
      <out>/manifest.json       # schema hash, row counts, rulebook hash, ts
      <out>/tables/*.parquet    # optional, only if with_parquet=True

    Returns <out>. Raises PackageError on any failure.
    """
    out = Path(cfg.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "tables").mkdir(exist_ok=True)

    # 1. Primary: pg_dump custom format.
    dump_path = out / "db.dump"
    _pg_dump_custom(cfg.db_url, dump_path)

    # 2. Schema hash (from a schema-only plain dump, sha256).
    schema_hash = _schema_hash(cfg.db_url)

    # 3. Row counts per table.
    row_counts = _row_counts(cfg.db_url)

    # 4. Optional Parquet export.
    parquet_tables: list[str] = []
    if cfg.with_parquet:
        parquet_tables = _export_parquet(cfg.db_url, out / "tables", row_counts.keys())

    # 5. manifest.json
    manifest: dict[str, Any] = {
        "source": "odoo_synth_snapshot",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "schema_hash": schema_hash,
        "row_counts": row_counts,
        "rulebook_hash": _rulebook_hash(cfg.rulebook_dir) if cfg.rulebook_dir else None,
        "rulebook_strategies": len(rulebook.strategies) if rulebook else None,
        "rulebook_field_rules": len(rulebook.field_rules) if rulebook else None,
        "primary_artifact": "db.dump",
        "parquet_tables": parquet_tables,
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), "utf-8"
    )
    return out


# ---------------------------------------------------------------------------
# pg_dump
# ---------------------------------------------------------------------------



def _dump_db_url(db_url: str) -> str:
    """DB URL for pg_dump specifically (the masked scratch DB package()
    dumps). Honors ODOO_SYNTH_PACKAGE_DB_URL for the in-container socket form
    when pg_dump runs in-container via ODOO_SYNTH_PG_DUMP. Distinct from
    ODOO_SYNTH_DUMP_DB_URL (which the source-dump in adapters/self_hosted.py
    uses) because package dumps the *masked scratch*, not the source."""
    import os
    return os.environ.get("ODOO_SYNTH_PACKAGE_DB_URL", db_url)


def _pg_dump_binary() -> list[str]:
    """Resolve the pg_dump command, honoring ODOO_SYNTH_PG_DUMP.

    Set ODOO_SYNTH_PG_DUMP to a command prefix (e.g.
    'docker exec odoo-synth-postgres-anon pg_dump') to route pg_dump through
    a container when the host's pg_dump is a different major version than the
    DB server (pg_dump cannot dump a newer-server-major DB from an older
    client). Plain `pg_dump` on PATH is the default.
    """
    import os
    import shlex
    override = os.environ.get("ODOO_SYNTH_PG_DUMP")
    if override:
        return shlex.split(override)
    return ["pg_dump"]


def _pg_dump_custom(db_url: str, out_file: Path) -> None:
    # Pipe to stdout (not -f <path>) so it works through `docker exec`
    # redirects where -f would be container-relative.
    proc = subprocess.run(
        _pg_dump_binary() + ["-Fc", _dump_db_url(db_url)],
        stdout=open(out_file, "wb"), stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        raise PackageError(
            f"pg_dump -Fc failed (exit {proc.returncode}): "
            f"{proc.stderr.decode('utf-8', 'replace')}"
        )


def _schema_hash(db_url: str) -> str:
    """sha256 of a schema-only plain-text dump -- a stable identity of the
    masked DB's structure, independent of row data."""
    proc = subprocess.run(
        _pg_dump_binary() + ["--schema-only", "--no-owner", "--no-privileges", _dump_db_url(db_url)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise PackageError(f"pg_dump --schema-only failed:\n{proc.stderr}")
    return hashlib.sha256(proc.stdout.encode("utf-8")).hexdigest()


def _row_counts(db_url: str) -> dict[str, int]:
    """Count rows per table in the public schema."""
    import psycopg
    counts: dict[str, int] = {}
    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT relname FROM pg_class c JOIN pg_namespace n "
                "ON c.relnamespace=n.oid WHERE n.nspname='public' "
                "AND c.relkind='r' ORDER BY relname"
            )
            tables = [r[0] for r in cur.fetchall()]
            for tbl in tables:
                cur.execute(f'SELECT count(*) FROM "{tbl}"')
                counts[tbl] = cur.fetchone()[0]
    return counts


def _rulebook_hash(rules_dir: Path) -> str:
    """sha256 over the concatenated rulebook files (sorted by name)."""
    h = hashlib.sha256()
    for f in sorted(rules_dir.glob("*.yml")):
        h.update(f.read_bytes())
        h.update(b"\x00")
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Parquet export (optional, DuckDB)
# ---------------------------------------------------------------------------


def _export_parquet(db_url: str, out_dir: Path, tables) -> list[str]:
    """Export each table to <out_dir>/<table>.parquet via DuckDB.

    Requires the `duckdb` extra (`pip install odoo-synth[parquet]`). Fails
    with a clear message if duckdb isn't importable -- never silently
    produces a partial artifact.
    """
    try:
        import duckdb  # type: ignore
    except ImportError as exc:
        raise PackageError(
            "--with-parquet requires the DuckDB extra: "
            "`pip install odoo-synth[parquet]`. " + str(exc)
        ) from exc
    # DuckDB can attach a Postgres DB via the postgres extension, but that's
    # not built into the pip wheel reliably across versions. The portable
    # path: read each table via psycopg into a Python list, register with
    # DuckDB, COPY to parquet. For large tables this isn't optimal, but the
    # Parquet export is explicitly secondary/optional per AGENT_PROMPT.md.
    # DuckDB's pip wheel doesn't reliably ship a Postgres scanner extension,
    # so we bridge via psycopg -> pandas DataFrame -> DuckDB COPY TO parquet.
    # The Parquet export is explicitly secondary/optional (AGENT_PROMPT.md),
    # so this non-optimal path is acceptable; the primary pg_dump artifact
    # is unaffected.
    try:
        import pandas as pd  # type: ignore
    except ImportError as exc:
        raise PackageError(
            "--with-parquet needs pandas (for the Postgres->parquet bridge) "
            "in addition to duckdb. `pip install pandas`."
        ) from exc
    import psycopg
    written: list[str] = []
    con = duckdb.connect()
    with psycopg.connect(db_url) as pg:
        with pg.cursor() as cur:
            for tbl in tables:
                cur.execute(f'SELECT * FROM "{tbl}"')
                rows = cur.fetchall()
                cols = [d[0] for d in cur.description]
                df = pd.DataFrame(rows, columns=cols)
                con.register("df", df)
                out_file = out_dir / f"{tbl}.parquet"
                con.execute(f"COPY (SELECT * FROM df) TO '{out_file}' (FORMAT PARQUET)")
                written.append(tbl)
    return written
