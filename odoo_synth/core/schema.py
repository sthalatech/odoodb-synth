"""Schema snapshot of an Odoo-shaped database.

A schema snapshot is a lossy, schema-only description of the tables and
columns in a bundle's database: table -> column -> {data_type, fk_target}.
It's the oracle `rules scan` / `rules diff` compare the rulebook against,
so the rulebook doesn't silently rot as an instance installs new modules.

Two producers, one shape:

  * package.package() emits ``schema.json`` from the live masked scratch DB
    via the catalogs (pg_class/pg_attribute/pg_constraint). This is the
    self-hosted path.
  * odoo_sh.ingest() parses ``schema.json`` from a bundle's dump.sql when
    present (odoo.sh backups ship a plain SQL dump). This is a best-effort
    parse that only needs to be good enough to drive the PII-shape
    classifier -- when it can't parse a statement it skips it, and
    coverage.py reports the unparsed tables so the operator knows the
    snapshot is incomplete rather than quietly trusting an empty result.

The shape is intentionally minimal: table name, column name, PostgreSQL
data type (as ``format_type(atttypid, atttypmod)``), and -- when the column
is a FK -- the target ``table.column``. The PII-shape classifier in
coverage.py maps types to shapes (free-text vs identifier vs binary) and
combines that with the FK target (a FK into res.partner is always
PII-shaped) to decide what ``rules scan`` should flag.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class ColumnInfo:
    name: str
    data_type: str
    # "table.column" if this column is a FK, else None.
    fk_target: str | None = None
    # True if this column is NOT NULL (helps the classifier weight a
    # required Many2one partner ref as higher-risk than an optional one).
    not_null: bool = False


@dataclass
class SchemaSnapshot:
    # table -> {column -> ColumnInfo}
    tables: dict[str, dict[str, ColumnInfo]] = field(default_factory=dict)
    # tables whose CREATE TABLE statement we couldn't parse (odoo.sh path
    # only); empty for the catalog-based path. Surfaced by coverage.py so an
    # incomplete snapshot is visible, not silently trusted.
    unparsed_tables: list[str] = field(default_factory=list)
    source: str = ""  # "pg_catalog" or "dump_sql_parse"

    def to_json(self) -> str:
        """Serialize for manifest/bundle sidecar."""
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "tables": {
                tbl: {col: asdict(ci) for col, ci in cols.items()}
                for tbl, cols in self.tables.items()
            },
            "unparsed_tables": sorted(self.unparsed_tables),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SchemaSnapshot":
        snap = cls(source=d.get("source", ""))
        for tbl, cols in (d.get("tables") or {}).items():
            snap.tables[tbl] = {}
            for col, ci in cols.items():
                snap.tables[tbl][col] = ColumnInfo(
                    name=ci["name"],
                    data_type=ci["data_type"],
                    fk_target=ci.get("fk_target"),
                    not_null=ci.get("not_null", False),
                )
        snap.unparsed_tables = list(d.get("unparsed_tables") or [])
        return snap


def load_snapshot(path: str | Path) -> SchemaSnapshot:
    """Load a schema.json sidecar from a bundle."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"schema snapshot not found: {p}")
    return SchemaSnapshot.from_dict(json.loads(p.read_text("utf-8")))


def snapshot_from_db(db_url: str) -> SchemaSnapshot:
    """Build a SchemaSnapshot from a live Postgres via the catalogs.

    Covers the public schema (Odoo tables live there). FK targets come from
    pg_constraint. Uses psycopg. This is the producer package() uses for the
    self-hosted path.
    """
    import psycopg

    snap = SchemaSnapshot(source="pg_catalog")
    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT c.relname, a.attname, "
                "format_type(a.atttypid, a.atttypmod), a.attnotnull "
                "FROM pg_class c "
                "JOIN pg_namespace n ON c.relnamespace=n.oid "
                "JOIN pg_attribute a ON a.attrelid=c.oid "
                "WHERE n.nspname='public' AND c.relkind='r' "
                "AND a.attnum>0 AND NOT a.attisdropped "
                "ORDER BY c.relname, a.attnum"
            )
            for tbl, col, dtype, notnull in cur.fetchall():
                snap.tables.setdefault(tbl, {})[col] = ColumnInfo(
                    name=col, data_type=dtype or "", not_null=bool(notnull),
                )
            # FK targets: conrel -> confrel, with attnum->attname maps.
            cur.execute(
                "SELECT conrelid::regclass::text, conname, "
                "confrelid::regclass::text, conkey, confkey "
                "FROM pg_constraint WHERE contype='f' "
                "AND connamespace='public'::regnamespace"
            )
            rows = cur.fetchall()
            for conrel, conname, confrel, conkey, confkey in rows:
                tbl = conrel.split(".")[-1].strip('"')
                fktbl = confrel.split(".")[-1].strip('"') if confrel else None
                if not fktbl or tbl not in snap.tables:
                    continue
                ref_cols = {}
                if confkey:
                    with conn.cursor() as c2:
                        c2.execute(
                            "SELECT attname, attnum FROM pg_attribute "
                            "WHERE attrelid=%s::regclass AND attnum=ANY(%s)",
                            (confrel, list(confkey)),
                        )
                        ref_cols = {num: name for name, num in c2.fetchall()}
                local_cols = {}
                if conkey:
                    with conn.cursor() as c2:
                        c2.execute(
                            "SELECT attname, attnum FROM pg_attribute "
                            "WHERE attrelid=%s::regclass AND attnum=ANY(%s)",
                            (conrel, list(conkey)),
                        )
                        local_cols = {num: name for name, num in c2.fetchall()}
                for i, lnum in enumerate(conkey or []):
                    lname = local_cols.get(lnum)
                    rnum = (confkey or [None])[i] if i < len(confkey or []) else None
                    rname = ref_cols.get(rnum) if rnum else None
                    if (lname and rname and tbl in snap.tables
                            and lname in snap.tables[tbl]):
                        snap.tables[tbl][lname].fk_target = f"{fktbl}.{rname}"
    return snap


# ---------------------------------------------------------------------------
# dump.sql parser (odoo.sh path -- best effort)
# ---------------------------------------------------------------------------

# CREATE TABLE "tbl" ( ... );  across multiple lines.
_CREATE_RE = re.compile(
    r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:public\.)?"?([A-Za-z0-9_]+)"?\s*\((.*?)\);',
    re.IGNORECASE | re.DOTALL,
)
_COL_RE = re.compile(r'^\s*"?([A-Za-z0-9_]+)"?\s+(.+)$', re.DOTALL)
_FK_RE = re.compile(
    r'REFERENCES\s+(?:public\.)?"?([A-Za-z0-9_]+)"?\s*(?:\(([^)]*)\))?',
    re.IGNORECASE,
)
_CONSTRAINT_START = re.compile(
    r'^(CONSTRAINT|PRIMARY|FOREIGN|UNIQUE|CHECK|EXCLUDE)\b', re.IGNORECASE
)


def snapshot_from_dump_sql(sql_text: str) -> SchemaSnapshot:
    """Best-effort parse of a plain-text pg_dump dump.sql.

    Only good enough to feed the PII-shape classifier. Skips statements it
    can't parse and records the table name in unparsed_tables so coverage.py
    can report an incomplete snapshot rather than silently trusting an empty
    result. FK targets come from inline REFERENCES clauses (pg_dump emits the
    real FKs as ALTER TABLE ADD CONSTRAINT FOREIGN KEY, which this parser
    does NOT yet follow -- so FK coverage from dump.sql is weaker than the
    catalog path; acceptable because the type-based classifier already catches
    the high-value free-text/identifier columns).
    """
    snap = SchemaSnapshot(source="dump_sql_parse")
    for m in _CREATE_RE.finditer(sql_text):
        tbl = m.group(1)
        body = m.group(2)
        cols: dict[str, ColumnInfo] = {}
        parsed_any = False
        for stmt in _split_top_commas(body):
            stmt = stmt.strip()
            if not stmt or _CONSTRAINT_START.match(stmt):
                continue
            cm = _COL_RE.match(stmt)
            if not cm:
                continue
            colname = cm.group(1)
            rest = cm.group(2).strip()
            dtype, fk_target, notnull = _parse_col_rest(rest)
            cols[colname] = ColumnInfo(
                name=colname, data_type=dtype,
                fk_target=fk_target, not_null=notnull,
            )
            parsed_any = True
        if parsed_any:
            snap.tables[tbl] = cols
        else:
            snap.unparsed_tables.append(tbl)
    return snap


def _split_top_commas(body: str) -> list[str]:
    """Split a CREATE TABLE body on commas at paren-depth 0."""
    out: list[str] = []
    depth = 0
    cur: list[str] = []
    for ch in body:
        if ch == "(":
            depth += 1
            cur.append(ch)
        elif ch == ")":
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if cur:
        out.append("".join(cur))
    return out


def _parse_col_rest(rest: str) -> tuple[str, str | None, bool]:
    """Parse the part after a column name: (data_type, fk_target, not_null)."""
    notnull = bool(re.search(r'\bNOT\s+NULL\b', rest, re.IGNORECASE))
    fkm = _FK_RE.search(rest)
    fk_target = None
    if fkm:
        ref_tbl = fkm.group(1)
        # Odoo FKs target the PK 'id'; the inline REFERENCES col list isn't
        # reliably present without the ALTER ADD CONSTRAINT, so assume id.
        fk_target = f"{ref_tbl}.id"
    stop = re.search(
        r'\b(NOT\s+NULL|NULL|DEFAULT|PRIMARY|REFERENCES|GENERATED|COLLATE)\b',
        rest, re.IGNORECASE,
    )
    dtype = rest[: stop.start()].strip() if stop else rest.strip()
    return dtype, fk_target, notnull
