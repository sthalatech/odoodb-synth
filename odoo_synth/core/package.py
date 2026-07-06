"""Package the masked result into a restorable artifact.

Primary artifact: a pg_dump custom-format file.
Secondary artifact (optional, --with-parquet): per-table Parquet export
via DuckDB. DuckDB is behind the `[parquet]` optional extra so the core
tool doesn't require it.

P1 item #11 -- not implemented yet.
"""

from __future__ import annotations

from pathlib import Path


def package(
    scratch_db_url: str,
    out: Path,
    with_parquet: bool = False,
) -> Path:
    """TODO P1 #11: produce the output artifact from a masked scratch DB.

    Primary: `pg_dump -Fc <scratch_db> -f <out>/db.dump`.
    Secondary (with_parquet=True): export each table to Parquet via DuckDB,
    requiring the `duckdb` extra (`pip install odoo-synth[parquet]`).
    """
    raise NotImplementedError(
        "package.package is not implemented yet -- P1 item #11. It will "
        "produce a pg_dump custom-format file (primary) and, with "
        "--with-parquet, a per-table Parquet export via DuckDB (secondary, "
        "requires the [parquet] extra)."
    )
