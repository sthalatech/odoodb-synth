"""Generate SECURITY LABEL statements from the loaded rulebook and apply them.

GUARDRAIL (from AGENT_PROMPT.md): never mask in place. apply_masking() only
ever runs against a scratch database restored from the source dump, never the
source itself. The caller hands us a scratch connection; we don't open one.

== How anon SECURITY LABELs work (confirmed against anon 3.1.3) ==

postgresql_anonymizer maps a masking rule to a column via:

    SECURITY LABEL FOR anon ON COLUMN <table>.<column> IS '<template>';

The <template> is the rulebook's `sql_template` with {column} substituted.
Calling `SELECT anon.anonymize_database();` then rewrites every labeled column
in place. Two gotchas we handle explicitly:

1. The label value is a SQL string literal, so an inner string literal in the
   template (e.g. seeded(..., 'anon.fake_first_name() || ...')) would collide
   with the outer quotes. We wrap the whole label in PG dollar-quoting
   ($$...$$) instead, which preserves the rulebook's single-quoted literals
   verbatim. Verified: this is the only form anon's label parser accepts for
   the seeded()-based strategies.

2. anon requires masking functions to be schema-qualified (or live in a
   TRUSTED schema). We mark the odoo_synth schema TRUSTED via a SECURITY LABEL
   on the schema, so odoo_synth.* functions resolve. Two rulebook templates
   call functions in pg_catalog without qualifying them (date_trunc); anon
   rejects bare `date_trunc(...)` with "not qualified". We qualify those
   known built-ins to pg_catalog. at render time rather than editing the
   verbatim rulebook.

== Strategies that are NOT per-column labels ==

* `keep` -> no label (skip).
* `shuffle_within_column` -> applied AFTER anonymize_database() via
  anon.shuffle_column() per sql/apply_shuffles.sql. Cross-row, not per-value.
* `rotate_secret` -> explicit UPDATE statements (fresh random values, not
  derived from the source) -- see rules/60_system_secrets.yml. These are
  hand-specified, not a generic label pattern.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import psycopg

from .rulebook import Rulebook
from .coverage import classify_shape, match_column_pattern

# ---------------------------------------------------------------------------

# Built-in PG functions the rulebook references unqualified. anon's label
# parser requires schema-qualification; we qualify these to pg_catalog at
# render time. This is a documented, minimal transform -- it does NOT change
# the rulebook YAML (which is verbatim), only the rendered label. Extend
# this set only if a new rulebook template introduces another bare built-in.
_BUILTIN_FN_QUALIFY = {
    "date_trunc": "pg_catalog.date_trunc",
}

# Tables that get row-level attachment scrubbing per rules/50_attachments.yml.
# These come from the rulebook's `full_scrub_filename_for_models` list, read
# at runtime from 50_attachments.yml's raw doc (the rulebook loader keeps the
# non-field-rule structure under Rulebook.raw).

ATTACHMENTS_FILE = "50_attachments.yml"


class MaskError(Exception):
    """Raised when masking cannot proceed (e.g. table missing, bad rulebook)."""


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def model_to_table(model: str) -> str:
    """Odoo model -> physical table name: replace '.' with '_'.

    res.partner -> res_partner. This covers core Odoo models. KNOWN GAP:
    some OCA/enterprise modules override the physical table name via
    _table = '...'; this mapping will produce a wrong (non-existent) table
    name for those and the SECURITY LABEL will error at apply time. That's a
    loud failure, not a silent one -- we don't try to guess the override. If
    you hit it, the fix is an explicit (model, table) override map, added
    here; do NOT silently skip.
    """
    return model.replace(".", "_")


def _qualify_builtins(template: str) -> str:
    """Qualify bare built-in function names the rulebook leaves unqualified.

    anon's label parser rejects unqualified function calls. Only date_trunc
    appears in the verbatim rulebook today; qualifying it to pg_catalog is
    a no-op semantically (date_trunc is in pg_catalog) and is required for
    the label to parse. We match `\\bdate_trunc(` only when not preceded by a
    dot, so an already-qualified `pg_catalog.date_trunc(` is left alone.
    """
    out = template
    for bare, qual in _BUILTIN_FN_QUALIFY.items():
        out = re.sub(r"(?<![\w.])\b" + re.escape(bare) + r"\s*\(", qual + "(", out)
    return out


def render_label(model: str, field: str, strategy_template: str) -> str:
    """Render a rulebook sql_template into a SECURITY LABEL statement.

    The returned string is a complete `SECURITY LABEL FOR anon ON COLUMN ...`
    statement. The label value is dollar-quoted ($$...$$) so the template's
    own single-quoted string literals survive verbatim.

    Odoo 19 stores several formerly-text fields as `jsonb` (translated fields
    became JSONB). `anon.partial(text, ...)` only accepts `text`, so a label
    like `anon.partial(barcode, ...)` on a jsonb `res_partner.barcode` raises
    `function anon.partial(jsonb, integer, unknown, integer) does not exist`.
    The rulebook is verbatim, so we cast the column to text here -- the only
    anon function in the shipped strategies that takes a bare text arg and
    can land on a non-text column is `anon.partial`. (The `odoo_synth.*`
    helpers and `anon.fake_*` helpers either accept anytype via their own
    casts or only get applied to text-like columns per the rulebook.)
    """
    table = model_to_table(model)
    rendered = strategy_template.replace("{column}", field)
    rendered = _qualify_builtins(rendered)
    # anon.partial({field}, ...) -> anon.partial({field}::text, ...) so a
    # jsonb/varchar column is accepted by anon.partial(text, ...).
    rendered = re.sub(
        r"(anon\.partial\()" + re.escape(field) + r"\b(,)",
        r"\1" + field + r"::text\2",
        rendered,
    )
    # Dollar-quote the whole label value. A template can't legitimately
    # contain $$ (no rulebook strategy uses it), so this is collision-free.
    return f"SECURITY LABEL FOR anon ON COLUMN {table}.{field} IS $${rendered}$$;"


@dataclass
class MaskPlan:
    """What apply_masking() will do, broken out for inspection/tests."""

    label_statements: list[str]
    shuffle_rules: list[tuple[str, str]]  # (model, field)
    rotate_secret_rules: list[tuple[str, str]]  # (model, field) from 60_*.yml
    attachment_full_scrub_models: list[str]


def build_plan(rulebook: Rulebook) -> MaskPlan:
    """Translate a loaded+validated Rulebook into a concrete MaskPlan.

    Splits field rules into:
      * label strategies (sql_template non-null, not shuffle/rotate)
      * shuffle_within_column rules (applied post-anonymize via shuffle_column)
      * rotate_secret rules (explicit UPDATEs, hand-specified)

    Plus two special structures from 60_system_secrets.yml that aren't
    under `fields:` and so aren't in rb.field_rules:

      * ir.config_parameter.match_by_key_regex -> a regex-based UPDATE that
        nulls `value` for every ir_config_parameter row whose `key` matches
        (drops any secret-shaped config param the rulebook didn't name
        individually).
      * ir.config_parameter.always_rotate_regardless_of_match -> named keys
        (database.secret, database.uuid, ...) that get fresh rotated values
        regardless of the regex. These are keyed by parameter *key*, not DB
        column -- surfaced as ('ir.config_parameter', '<key>') rotate rules.

    Reads 50_attachments.yml's `full_scrub_filename_for_models` list from the
    raw doc (kept by the loader) for the attachment filename scrub pass.
    """
    labels: list[str] = []
    shuffles: list[tuple[str, str]] = []
    rotates: list[tuple[str, str]] = []

    for (model, field), fr in rulebook.field_rules.items():
        strat = rulebook.strategies.get(fr.strategy)
        if strat is None:
            # validate() already catches this, but be defensive.
            raise MaskError(
                f"{fr.file}: {model}.{field} uses unknown strategy {fr.strategy!r}"
            )
        if fr.strategy == "keep":
            continue
        if fr.strategy == "shuffle_within_column":
            shuffles.append((model, field))
            continue
        if fr.strategy == "rotate_secret":
            rotates.append((model, field))
            continue
        if strat.sql_template is None:
            # keep/shuffle are the only null-template strategies; anything
            # else with a null template is a rulebook bug.
            raise MaskError(
                f"{fr.file}: {model}.{field} strategy {fr.strategy!r} has no "
                "sql_template and isn't a recognized special strategy."
            )
        labels.append(render_label(model, field, strat.sql_template))

    # ir.config_parameter special structure (60_system_secrets.yml):
    # match_by_key_regex (drop value where key matches) + always_rotate.
    secrets_doc = rulebook.raw.get("60_system_secrets.yml", {}) or {}
    ir_param = secrets_doc.get("ir.config_parameter", {}) if isinstance(secrets_doc, dict) else {}
    if isinstance(ir_param, dict):
        regex = ir_param.get("match_by_key_regex")
        if isinstance(regex, str) and regex:
            rotates.append(("__ir_config_parameter_regex__", regex))
        always = ir_param.get("always_rotate_regardless_of_match", {}) or {}
        if isinstance(always, dict):
            for key in always:
                rotates.append(("ir.config_parameter", str(key)))

    # Attachment filename scrub models from 50_attachments.yml (raw structure).
    att_models: list[str] = []
    att_doc = rulebook.raw.get(ATTACHMENTS_FILE, {}) or {}
    if isinstance(att_doc, dict):
        m = att_doc.get("full_scrub_filename_for_models") or []
        if isinstance(m, list):
            att_models = [str(x) for x in m]

    return MaskPlan(
        label_statements=labels,
        shuffle_rules=shuffles,
        rotate_secret_rules=rotates,
        attachment_full_scrub_models=att_models,
    )


def _read_app_columns(cur) -> list[tuple[str, str, str, str | None]]:
    """Read (schema, table, column, data_type, fk_target) for every column in
    every app schema (public + odoo_synth) from the live scratch catalogs.

    The anon extension installs a DDL event trigger that can relocate
    newly-created tables into the odoo_synth schema; real Odoo tables
    restored from a dump stay in public. Reading both (and excluding only
    system schemas + the anon faker-dictionary tables) means the pattern
    backstop sees the same columns `rules scan` would, regardless of which
    schema they landed in. fk_target is resolved from pg_constraint.
    """
    cur.execute(
        "SELECT n.nspname, c.relname, a.attname, "
        "format_type(a.atttypid, a.atttypmod) "
        "FROM pg_class c "
        "JOIN pg_namespace n ON c.relnamespace=n.oid "
        "JOIN pg_attribute a ON a.attrelid=c.oid "
        "WHERE c.relkind='r' AND a.attnum>0 AND NOT a.attisdropped "
        "AND n.nspname IN ('public','odoo_synth') "
        "ORDER BY n.nspname, c.relname, a.attnum"
    )
    base = cur.fetchall()  # (nsp, tbl, col, dtype)
    # Resolve FK targets for partner_ref classification.
    cur.execute(
        "SELECT conrelid::regclass::text, confrelid::regclass::text, "
        "conkey, confkey FROM pg_constraint WHERE contype='f'"
    )
    fk_map: dict[tuple[str, str], str] = {}  # (tbl, col) -> "fktbl.col"
    for conrel, confrel, conkey, confkey in cur.fetchall():
        tbl = conrel.split(".")[-1].strip('"')
        fktbl = confrel.split(".")[-1].strip('"') if confrel else None
        if not fktbl:
            continue
        # map local attnum -> local colname
        with cur.connection.cursor() as c2:
            c2.execute(
                "SELECT attname, attnum FROM pg_attribute "
                "WHERE attrelid=%s::regclass AND attnum=ANY(%s)",
                (conrel, list(conkey or [])),
            )
            local = {num: name for name, num in c2.fetchall()}
        with cur.connection.cursor() as c2:
            c2.execute(
                "SELECT attname, attnum FROM pg_attribute "
                "WHERE attrelid=%s::regclass AND attnum=ANY(%s)",
                (confrel, list(confkey or [])),
            )
            ref = {num: name for name, num in c2.fetchall()}
        for i, lnum in enumerate(conkey or []):
            lname = local.get(lnum)
            rnum = (confkey or [None])[i] if i < len(confkey or []) else None
            rname = ref.get(rnum) if rnum else None
            if lname and rname:
                fk_map[(tbl, lname)] = f"{fktbl}.{rname}"
    out = []
    for nsp, tbl, col, dtype in base:
        out.append((tbl, col, dtype or "", fk_map.get((tbl, col))))
    return out


def build_pattern_labels_rows(
    rulebook: Rulebook, rows: list[tuple[str, str, str, str | None]]
) -> tuple[list[str], int, int]:
    """Generate SECURITY LABEL statements for columns matching a rulebook
    column-name pattern but not already covered by an explicit field rule.

    Masking-side counterpart to coverage.match_column_pattern(): where
    `rules scan` *counts* pattern-covered columns (visible, not flagged),
    masking *applies* a real anon label to each so the denormalized caches
    (account_move.invoice_partner_display_name, res_partner.complete_name,
    ir_sequence.name, ...) actually get masked.

    An explicit per-model field rule always wins: skip any (model, col)
    pair already in rulebook.field_rules. Returns (label_statements, matched,
    skipped_implicit) -- implicit_skipped counts columns a pattern would have
    matched but an explicit rule already covered (overlap visibility).
    """
    from .schema import ColumnInfo
    from .coverage import _build_table_model_index, _table_to_model
    labels: list[str] = []
    matched = 0
    implicit_skipped = 0
    explicit = set(rulebook.field_rules.keys())
    table_index = _build_table_model_index(rulebook)
    for tbl, col, dtype, fk in rows:
        model = _table_to_model(tbl, table_index)
        if (model, col) in explicit:
            implicit_skipped += 1
            continue
        ci = ColumnInfo(name=col, data_type=dtype, fk_target=fk)
        shape = classify_shape(ci)
        pat = match_column_pattern(col, shape, rulebook.column_patterns)
        if pat is None:
            continue
        strat = rulebook.strategies.get(pat.strategy)
        if strat is None or strat.sql_template is None:
            continue  # validate() guards this; defensive skip.
        matched += 1
        labels.append(render_label(model, col, strat.sql_template))
    return labels, matched, implicit_skipped


def build_pattern_labels(rulebook: Rulebook, snapshot) -> tuple[list[str], int, int]:
    """Convenience wrapper for callers that already hold a SchemaSnapshot
    (e.g. tests). The masking pipeline uses build_pattern_labels_rows +
    _read_app_columns instead, to be schema-relocation-robust."""
    from .coverage import _build_table_model_index, _table_to_model
    labels: list[str] = []
    matched = 0
    implicit_skipped = 0
    explicit = set(rulebook.field_rules.keys())
    table_index = _build_table_model_index(rulebook)
    rows = []
    for tbl, cols in snapshot.tables.items():
        for col, ci in cols.items():
            rows.append((tbl, col, ci.data_type, ci.fk_target))
    return build_pattern_labels_rows(rulebook, rows)


# ---------------------------------------------------------------------------
# Attachment policy (rules/50_attachments.yml)
# ---------------------------------------------------------------------------


def attachment_policy(rulebook: Rulebook) -> dict[str, Any]:
    """Parse the default attachment policy + scrub model list from 50_*.yml.

    Returns {'content': 'drop'|'keep', 'index_content': ..., 'filename': ...,
    'full_scrub_models': [str], 'exclude_models': [str]}.
    """
    doc = rulebook.raw.get(ATTACHMENTS_FILE, {}) or {}
    if not isinstance(doc, dict):
        return {"full_scrub_models": [], "exclude_models": []}
    dp = doc.get("default_policy", {}) or {}
    scrub = doc.get("full_scrub_filename_for_models", []) or []
    excl = doc.get("exclude_rows_entirely_for_models", []) or []
    return {
        "default_policy": dp if isinstance(dp, dict) else {},
        "full_scrub_models": [str(x) for x in scrub] if isinstance(scrub, list) else [],
        "exclude_models": [str(x) for x in excl] if isinstance(excl, list) else [],
    }


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------


def _exists(cur, table: str) -> bool:
    cur.execute(
        "SELECT to_regclass(%s) IS NOT NULL", (table,)
    )
    return bool(cur.fetchone()[0])


def _exec_script(cur, statements: Iterable[str]) -> tuple[int, int]:
    """Run a list of SQL statements, skipping ones that error on missing tables.

    Returns (applied, skipped). A SECURITY LABEL on a table that doesn't
    exist in this Odoo instance (e.g. payment_provider when the payment
    module isn't installed) errors -- we skip those and count them, rather
    than aborting the whole pass. Non-existence errors are expected across
    instances with different module sets.
    """
    applied = 0
    skipped = 0
    for stmt in statements:
        try:
            cur.execute(stmt)
            applied += 1
        except psycopg.Error as exc:
            # "relation ... does not exist" -> skip. Other errors (bad
            # template, syntax) must NOT be swallowed.
            msg = str(exc).lower()
            if "does not exist" in msg or "cannot be found" in msg:
                skipped += 1
                continue
            raise MaskError(f"masking statement failed: {stmt}\n  -> {exc}") from exc
    return applied, skipped


def apply_masking(scratch_db_url: str, rulebook: Rulebook) -> dict[str, Any]:
    """Apply the full masking pipeline to a SCRATCH database.

    Never call this against a source DB. The caller is responsible for
    handing us a scratch DB restored from the source dump (see
    adapters/self_hosted.py).

    Steps:
      0. Mark the odoo_synth schema TRUSTED so anon accepts our functions
         in masking labels.
      1. Apply per-column SECURITY LABELs for every non-keep/non-shuffle/
         non-rotate strategy. Skip labels for tables that don't exist in
         this instance (module not installed) -- count them.
      2. SELECT anon.anonymize_database(); to apply all labels in place.
      3. Run anon.shuffle_column() for each shuffle_within_column rule.
      4. Run explicit rotate_secret UPDATEs (60_system_secrets.yml): fresh
         random values, not derived from the source.
      5. Attachment pass: null ir_attachment content/index_content and
         scrub filenames for the configured full-scrub models.

    Returns a summary dict of what ran, for logging + tests.
    """
    plan = build_plan(rulebook)

    with psycopg.connect(scratch_db_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            # 0. Trust the odoo_synth schema so anon accepts our funcs.
            cur.execute("SECURITY LABEL FOR anon ON SCHEMA odoo_synth IS 'TRUSTED';")

            # 1. Per-column labels (explicit per-model field rules).
            labels_applied, labels_skipped = _exec_script(cur, plan.label_statements)

            # 1b. Column-name pattern labels (the denormalized-cache backstop).
            # Patterns apply to columns the explicit rules don't cover (e.g.
            # account_move.invoice_partner_display_name, ir_sequence.name).
            # We read the columns from the catalogs across ALL app schemas
            # (public + odoo_synth -- the anon DDL event trigger can relocate
            # newly-created tables to odoo_synth, so a public-only read would
            # miss them). Real restored-from-dump Odoo tables stay in public.
            pattern_labels: list[str] = []
            pattern_applied = 0
            pattern_skipped = 0
            if rulebook.column_patterns:
                try:
                    cols_rows = _read_app_columns(cur)
                except Exception as exc:  # pragma: no cover - catalog read
                    raise MaskError(
                        f"pattern-label schema read failed: {exc}"
                    ) from exc
                pattern_labels, _matched, _impl = build_pattern_labels_rows(
                    rulebook, cols_rows)
                pattern_applied, pattern_skipped = _exec_script(
                    cur, pattern_labels)

            # 2. Apply.
            cur.execute("SELECT anon.anonymize_database();")
            anon_result = cur.fetchone()[0]

            # 3. Shuffles (cross-row, post-anonymize).
            shuffle_applied = 0
            shuffle_skipped = 0
            for model, field in plan.shuffle_rules:
                table = model_to_table(model)
                if not _exists(cur, table):
                    shuffle_skipped += 1
                    continue
                # anon.shuffle_column(regclass, name, name) -- needs a PK.
                # We resolve the PK column dynamically from the table.
                cur.execute(
                    """
                    SELECT a.attname
                    FROM pg_index i
                    JOIN pg_attribute a ON a.attrelid = i.indrelid
                       AND a.attnum = ANY(i.indkey)
                    WHERE i.indrelid = %s::regclass AND i.indisprimary
                    ORDER BY array_position(i.indkey, a.attnum)
                    LIMIT 1
                    """,
                    (table,),
                )
                row = cur.fetchone()
                if row is None:
                    shuffle_skipped += 1
                    continue
                pk = row[0]
                try:
                    cur.execute(
                        "SELECT anon.shuffle_column(%s::regclass, %s::name, %s::name)",
                        (table, field, pk),
                    )
                    shuffle_applied += 1
                except psycopg.Error as exc:
                    # Column missing -> skip; anything else is a real error.
                    if "does not exist" in str(exc).lower():
                        shuffle_skipped += 1
                        continue
                    raise MaskError(
                        f"shuffle_column({table}, {field}, {pk}) failed: {exc}"
                    ) from exc

            # 4. Rotate secrets.
            rotate_applied, rotate_skipped = _apply_rotate_secrets(
                cur, plan.rotate_secret_rules, rulebook
            )

            # 5. Attachment pass.
            att = _apply_attachment_policy(cur, plan.attachment_full_scrub_models)

    return {
        "labels_applied": labels_applied,
        "labels_skipped": labels_skipped,
        "pattern_labels": len(pattern_labels) if rulebook.column_patterns else 0,
        "pattern_applied": pattern_applied,
        "pattern_skipped": pattern_skipped,
        "anonymize_database": anon_result,
        "shuffle_applied": shuffle_applied,
        "shuffle_skipped": shuffle_skipped,
        "rotate_applied": rotate_applied,
        "rotate_skipped": rotate_skipped,
        "attachment": att,
    }


# ---------------------------------------------------------------------------
# rotate_secret implementation
# ---------------------------------------------------------------------------

# Field-specific rotation logic. Each entry is (model, field) -> SQL expr
# yielding the fresh value. These are hand-specified per 60_system_secrets.yml
# -- rotate means "fresh random, NOT derived from the source value", so we
# do not read the old column value at all. Extend this map when adding new
# rotate_secret entries to the rulebook.
#
# ir.config_parameter is keyed by `key` (not by id), so these are conditional
# UPDATEs matching the key, not whole-column updates.
_ROTATE_SECRET_SQL = {
    ("ir.config_parameter", "database.secret"):
        "UPDATE ir_config_parameter SET value = {secret} WHERE key = 'database.secret'",
    ("ir.config_parameter", "database.uuid"):
        "UPDATE ir_config_parameter SET value = {uuid} WHERE key = 'database.uuid'",
    # payment.token.active -> force false (belt-and-suspenders w/ dropped provider_ref)
    ("payment.token", "active"):
        "UPDATE payment_token SET active = false",
    # payment.provider.state -> 'disabled'
    ("payment.provider", "state"):
        "UPDATE payment_provider SET state = 'disabled'",
    # hr.employee.pin -> fixed dev default (a string, not derived)
    ("hr.employee", "pin"):
        "UPDATE hr_employee SET pin = '0000'",
    # website.visitor.access_token -> fresh random token
    ("website.visitor", "access_token"):
        "UPDATE website_visitor SET access_token = {token}",
    # iap.account.account_token -> fresh random token (In-App Purchase credit
    # token; rotating prevents a dev cron from billing production IAP credits)
    ("iap.account", "account_token"):
        "UPDATE iap_account SET account_token = {token}",
}


def _fresh_secret_expr() -> str:
    """64-hex-char signing secret from two random uuids (no pgcrypto dep)."""
    return "md5(gen_random_uuid()::text) || md5(gen_random_uuid()::text)"


def _fresh_uuid_expr() -> str:
    return "gen_random_uuid()::text"


def _fresh_token_expr() -> str:
    return "md5(gen_random_uuid()::text) || md5(gen_random_uuid()::text)"


def _apply_rotate_secrets(
    cur, rules: list[tuple[str, str]], rulebook: Rulebook
) -> tuple[int, int]:
    """Run the rotate_secret UPDATEs + ir_config_parameter regex drop.

    `rules` may contain:
      * ('ir.config_parameter', '<key>') -- a named config parameter to
        rotate (database.secret, database.uuid, ...). Handled via the
        conditional UPDATEs in _ROTATE_SECRET_SQL keyed by the parameter key.
      * ('__ir_config_parameter_regex__', '<regex>') -- drop `value` for
        every ir_config_parameter row whose `key` matches the regex. This is
        the catch-all for secret-shaped keys the rulebook didn't name.
      * (model, field) for everything else -- table-column rotation.

    Skips rules whose target table is missing (module not installed). A
    rotate_secret rule with no matching implementation fails loud.
    """
    applied = 0
    skipped = 0
    for model, field in rules:
        # Special: regex-based config-parameter drop.
        if model == "__ir_config_parameter_regex__":
            if not _exists(cur, "ir_config_parameter"):
                skipped += 1
                continue
            try:
                # ir_config_parameter.value is NOT NULL in real Odoo 19
                # (unlike the demo schema this was developed against). Use
                # '' (cleared) instead of NULL so the UPDATE succeeds on a
                # real schema. Semantically equivalent: the secret value is
                # gone, the key remains so Odoo still finds the row. We keep
                # the NULL behavior when the column allows it (matches the
                # original demo-schema semantics and the test expectations)
                # so this stays backward-compatible.
                cur.execute(
                    "SELECT is_nullable FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name='ir_config_parameter' "
                    "AND column_name='value'"
                )
                row = cur.fetchone()
                # row is None when ir_config_parameter isn't in public (e.g.
                # the anon DDL trigger relocates it to the odoo_synth schema in
                # the test setup). Default to NULL in that case to preserve the
                # original demo-schema behavior.
                nullable = row is None or row[0] == "YES"
                set_clause = "value = NULL" if nullable else "value = ''"
                cur.execute(
                    f"UPDATE ir_config_parameter SET {set_clause} WHERE key ~ %s",
                    (field,),
                )
                applied += 1
            except psycopg.Error as exc:
                if "does not exist" in str(exc).lower():
                    skipped += 1
                    continue
                raise MaskError(
                    f"ir_config_parameter regex drop failed: {exc}"
                ) from exc
            continue

        table = model_to_table(model)
        key = (model, field)
        if key not in _ROTATE_SECRET_SQL:
            # A rotate_secret rule in the rulebook without a matching
            # implementation is a real gap -- fail loud, don't silently skip.
            raise MaskError(
                f"rotate_secret rule for {model}.{field} has no implementation in "
                "mask._ROTATE_SECRET_SQL. Add one (fresh random value, not derived "
                "from the source). See rules/60_system_secrets.yml."
            )
        if not _exists(cur, table):
            skipped += 1
            continue
        sql = _ROTATE_SECRET_SQL[key]
        if "{secret}" in sql:
            sql = sql.format(secret=_fresh_secret_expr())
        elif "{uuid}" in sql:
            sql = sql.format(uuid=_fresh_uuid_expr())
        elif "{token}" in sql:
            sql = sql.format(token=_fresh_token_expr())
        try:
            cur.execute(sql)
            applied += 1
        except psycopg.Error as exc:
            # Missing column (module version skew) -> skip; other errors fail.
            if "does not exist" in str(exc).lower():
                skipped += 1
                continue
            raise MaskError(
                f"rotate_secret UPDATE failed for {model}.{field}: {exc}\n  sql: {sql}"
            ) from exc
    return applied, skipped


# ---------------------------------------------------------------------------
# Attachment policy implementation
# ---------------------------------------------------------------------------


def _apply_attachment_policy(
    cur, full_scrub_models: list[str]
) -> dict[str, int]:
    """Apply the default attachment policy from 50_attachments.yml.

    Default policy drops: content (datas), checksum, index_content; keeps
    filename + mimetype + file_size. For models in full_scrub_models, also
    null/replace the filename. This is DB-row-level only -- the actual
    filestore blobs on disk are handled by the adapter that produced the
    bundle, not here.

    Resilient to column variation across Odoo versions: only nulls the
    content-bearing columns that actually exist on this instance's
    ir_attachment table (some versions/modules add or omit store_fname /
    checksum). Never aborts the whole pass because one column is absent.
    """
    summary = {
        "rows_content_dropped": 0,
        "rows_index_dropped": 0,
        "filenames_scrubbed": 0,
    }
    if not _exists(cur, "ir_attachment"):
        return summary

    # Default policy: null every content-bearing column that exists.
    # ir_attachment stores blob either inline in `datas` or as a filestore
    # reference in `store_fname`; null both to be safe, plus checksum +
    # index_content. We build the SET clause from the columns that actually
    # exist so a missing column (version skew) doesn't abort the pass.
    drop_cols = ["datas", "store_fname", "checksum", "index_content"]
    # Resolve the table via to_regclass (search-path aware) and read its
    # columns from pg_attribute. We do NOT filter by schema -- the anon
    # extension installs a DDL event trigger that can relocate newly-created
    # tables into the odoo_synth schema (a behavior of its replica-masking
    # machinery), so a hard-coded 'public' filter would miss them. Real Odoo
    # tables created before anon was loaded stay in public; either way
    # to_regclass + pg_attribute finds the columns.
    cur.execute("SELECT a.attname FROM pg_attribute a "
                "WHERE a.attrelid = to_regclass('ir_attachment') "
                "AND a.attnum > 0 AND NOT a.attisdropped")
    present = {r[0] for r in cur.fetchall()}
    set_cols = [c for c in drop_cols if c in present]
    if set_cols:
        set_clause = ", ".join(f"{c} = NULL" for c in set_cols)
        try:
            cur.execute(f"UPDATE ir_attachment SET {set_clause}")
            summary["rows_content_dropped"] = cur.rowcount
        except psycopg.Error as exc:
            if "does not exist" not in str(exc).lower():
                raise MaskError(f"attachment content drop failed: {exc}") from exc

    # Filename scrub for configured models: replace name with
    # attachment_<id>.<ext> so the original (PII-bearing) filename is gone
    # but the extension survives for UI debugging.
    if not full_scrub_models:
        return summary

    # Build the IN-list of res_model values from the model names.
    models_csv = ",".join("'" + m.replace("'", "''") + "'" for m in full_scrub_models)
    try:
        cur.execute(
            f"""
            UPDATE ir_attachment
            SET name = 'attachment_' || id::text ||
                COALESCE('.' || (regexp_match(name, '\\.[^.]+$'))[1], '')
            WHERE res_model IN ({models_csv})
              AND name IS NOT NULL
            """
        )
        summary["filenames_scrubbed"] = cur.rowcount
    except psycopg.Error as exc:
        # regexp_match is PG 10+; if something's off, fall back to no-ext
        # replacement so the PII-bearing filename is still gone.
        if "does not exist" in str(exc).lower():
            try:
                cur.execute(
                    f"""
                    UPDATE ir_attachment SET name = 'attachment_' || id::text
                    WHERE res_model IN ({models_csv}) AND name IS NOT NULL
                    """
                )
                summary["filenames_scrubbed"] = cur.rowcount
            except psycopg.Error as exc2:
                raise MaskError(f"attachment filename scrub failed: {exc2}") from exc2
        else:
            raise MaskError(f"attachment filename scrub failed: {exc}") from exc
    return summary


# ---------------------------------------------------------------------------
# Leak scan (used by tests + the CLI for a post-mask sanity gate)
# ---------------------------------------------------------------------------


def leak_scan(scratch_db_url: str, original_values: list[str]) -> list[str]:
    """Scan the scratch DB for any surviving original PII value.

    Returns the list of original values still present (empty == pass). This
    is the TESTING.md section 3 leak-scan gate, factored out so tests can call
    it on a small seeded scratch DB.

    Method (in priority order):
      1. `pg_dump` the DB to plain-text SQL and substring-search every
         original value. This is the spec'd method (TESTING.md section 3):
         it catches a value sitting anywhere, including in columns the
         rulebook didn't think to mask, in bytea hex, in indexes, etc.
      2. If pg_dump isn't on the local PATH (e.g. running tests from a host
         that only has the DB in a container), fall back to a SQL scan:
         concatenate the text representation of every column of every table
         in the public schema and substring-search that. This is weaker
         (won't see non-public schemas or bytea hex the same way) but
         container-portable and sufficient for the small seeded schema the
         unit/integration test uses.

    Both paths do a case-sensitive substring match (grep -F semantics).
    """
    found: list[str] = []
    vals = [v for v in original_values if v]
    if not vals:
        return found

    dump_text: str | None = None
    pg_dump_err: str | None = None
    try:
        dump_text = _pg_dump_text(scratch_db_url)
    except MaskError as exc:
        # Version mismatch (host pg_dump older than server) is the common
        # case in the scratch-stack setup -- fall back to the SQL scan rather
        # than failing the leak check outright.
        if "version" in str(exc).lower() and "mismatch" in str(exc).lower():
            pg_dump_err = str(exc)
        else:
            raise
    if dump_text is None:
        # pg_dump unavailable or version-incompatible -> SQL fallback.
        dump_text = _sql_scan_text(scratch_db_url)
    if dump_text is None:
        raise MaskError(
            "leak_scan: could not obtain DB text (pg_dump missing/incompatible "
            "and SQL fallback failed) -- install a matching pg_dump or set "
            "ODOO_SYNTH_PG_DUMP to a containerized one. "
            f"pg_dump error: {pg_dump_err}"
        )
    for v in vals:
        if v in dump_text:
            found.append(v)
    return found


def _pg_dump_binary() -> list[str]:
    """Resolve the pg_dump command, honoring ODOO_SYNTH_PG_DUMP (same pattern
    as core/package.py). The host's pg_dump may be an older major than the
    DB server (PG16 client can't dump PG18), so the container override is the
    portable path for the leak scan against the scratch stack."""
    import os
    import shlex
    override = os.environ.get("ODOO_SYNTH_PG_DUMP")
    if override:
        return shlex.split(override)
    return ["pg_dump"]


def _pg_dump_text(db_url: str) -> str | None:
    """Return pg_dump plain-text output, or None if pg_dump isn't available."""
    import shutil
    import subprocess
    import os
    binary = _pg_dump_binary()
    # Only check PATH availability for the default (non-overridden) pg_dump.
    if not os.environ.get("ODOO_SYNTH_PG_DUMP") and shutil.which(binary[0]) is None:
        return None
    # When using the container override, the DB URL also needs to be the
    # in-container form -- honor ODOO_SYNTH_DUMP_DB_URL (the masked scratch DB
    # is what we're scanning).
    dump_url = os.environ.get("ODOO_SYNTH_DUMP_DB_URL") or os.environ.get(
        "ODOO_SYNTH_PACKAGE_DB_URL") or db_url
    proc = subprocess.run(
        binary + ["--no-owner", "--no-privileges", "--schema=public", dump_url],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise MaskError(f"pg_dump for leak scan failed: {proc.stderr}")
    return proc.stdout


def _sql_scan_text(db_url: str) -> str | None:
    """Fallback: concatenate every user-schema table's rows to one string.

    Uses the text representation of every column (so bytea shows as hex via
    default text cast). We build one big text concatenation per table and
    read it back, then join. Container-portable (no pg_dump needed).

    Scans ALL non-system schemas (not just public) because the anon
    extension's DDL event trigger can relocate newly-created tables into
    the odoo_synth schema -- a public-only filter would miss them and the
    leak scan would false-pass. For real Odoo DBs the tables live in public;
    this covers both.
    """
    try:
        with psycopg.connect(db_url, autocommit=True) as conn:
            with conn.cursor() as cur:
                # Resolve table names via to_regclass (search-path aware) for
                # every table in user schemas, then dump each via its
                # schema-qualified name so unqualified-name collisions across
                # schemas don't confuse the readback.
                # Scan app schemas (public, odoo_synth) but NOT the anon
                # extension's faker-dictionary tables (anon.address, anon.postcode,
                # ...), which legitimately contain real-looking values that would
                # false-positive. The anon dictionary is reference data for the
                # fake_* generators, not customer data.
                cur.execute(
                    "SELECT n.nspname, c.relname FROM pg_class c "
                    "JOIN pg_namespace n ON c.relnamespace=n.oid "
                    "WHERE c.relkind='r' "
                    "AND n.nspname NOT IN ('pg_catalog','information_schema',"
                    "'anon') "
                    "ORDER BY n.nspname, c.relname"
                )
                tables = [(r[0], r[1]) for r in cur.fetchall()]
                chunks: list[str] = []
                for nsp, tbl in tables:
                    fq = f'"{nsp}"."{tbl}"'
                    try:
                        cur.execute(
                            f"SELECT string_agg(t::text, E'\n') FROM {fq} t"
                        )
                    except psycopg.Error:
                        continue  # system/extension table we can't read
                    row = cur.fetchone()
                    if row and row[0]:
                        chunks.append(str(row[0]))
                return "\n".join(chunks)
    except psycopg.Error as exc:
        raise MaskError(f"SQL fallback scan failed: {exc}") from exc
