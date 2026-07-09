"""Unit tests for the rulebook-coverage engine (rules scan/diff).

These don't need a database -- the dump.sql parser produces a schema
snapshot from a string, and the coverage analyzer is pure logic over a
Rulebook + SchemaSnapshot. The live-DB path (snapshot_from_db) is exercised
by tests/integration/test_scan_cli.py.
"""

from __future__ import annotations

import pytest

from odoo_synth.core.coverage import analyze, classify_shape
from odoo_synth.core.rulebook import load_and_validate
from odoo_synth.core.schema import ColumnInfo, SchemaSnapshot, snapshot_from_dump_sql


# A tiny synthetic dump.sql covering: a fully-declared model (res.partner),
# a partially-declared model (crm.lead -- name/partner_id uncovered on
# purpose), and an undeclared model (x.custom.ticket). This mirrors the
# real rulebook's actual coverage so the findings are meaningful, not
# tautological.
_DUMP = """
CREATE TABLE res_partner (
    id serial PRIMARY KEY,
    name character varying NOT NULL,
    email character varying,
    phone character varying,
    street text,
    vat character varying,
    comment text,
    image_1920 bytea,
    partner_latitude numeric(15,8)
);

CREATE TABLE crm_lead (
    id serial PRIMARY KEY,
    name character varying NOT NULL,
    partner_id integer REFERENCES res_partner(id),
    email_from character varying,
    phone character varying,
    description text
);

CREATE TABLE x_custom_ticket (
    id serial PRIMARY KEY,
    subject text,
    customer_id integer REFERENCES res_partner(id),
    internal_note text,
    payload bytea
);
"""


@pytest.fixture(scope="module")
def rulebook():
    return load_and_validate("rules/")


@pytest.fixture(scope="module")
def snapshot():
    return snapshot_from_dump_sql(_DUMP)


# ---------------------------------------------------------------------------
# classify_shape -- the type/fk -> PII-shape mapping
# ---------------------------------------------------------------------------


def test_classify_shape_free_text():
    assert classify_shape(ColumnInfo("x", "text")) == "free_text"
    assert classify_shape(ColumnInfo("x", "character varying")) == "free_text"
    assert classify_shape(ColumnInfo("x", "varchar(255)")) == "free_text"


def test_classify_shape_binary():
    assert classify_shape(ColumnInfo("x", "bytea")) == "binary"


def test_classify_shape_partner_ref():
    assert classify_shape(ColumnInfo("x", "integer", fk_target="res_partner.id")) == "partner_ref"


def test_classify_shape_other_for_numeric_and_non_partner_fk():
    assert classify_shape(ColumnInfo("x", "numeric(15,8)")) == "other"
    assert classify_shape(ColumnInfo("x", "integer", fk_target="res_company.id")) == "other"
    assert classify_shape(ColumnInfo("x", "boolean")) == "other"


# ---------------------------------------------------------------------------
# snapshot_from_dump_sql -- the parser
# ---------------------------------------------------------------------------


def test_dump_parser_extracts_tables_and_columns(snapshot):
    assert set(snapshot.tables) == {"res_partner", "crm_lead", "x_custom_ticket"}
    assert "name" in snapshot.tables["res_partner"]
    assert snapshot.tables["res_partner"]["name"].not_null is True
    # Internal-comma type is parsed whole, not split at the comma.
    assert snapshot.tables["res_partner"]["partner_latitude"].data_type == "numeric(15,8)"
    # FK target resolved from inline REFERENCES.
    assert snapshot.tables["crm_lead"]["partner_id"].fk_target == "res_partner.id"


def test_dump_parser_records_unparsed_when_no_columns():
    # A CREATE TABLE with only constraints (no columns) -> unparsed.
    snap = snapshot_from_dump_sql(
        "CREATE TABLE weird (CONSTRAINT pk PRIMARY KEY (id));"
    )
    assert "weird" in snap.unparsed_tables
    assert "weird" not in snap.tables


# ---------------------------------------------------------------------------
# analyze -- the coverage report against the real rulebook
# ---------------------------------------------------------------------------


def test_analyze_flags_undeclared_model(rulebook, snapshot):
    report = analyze(rulebook, snapshot)
    # x.custom.ticket isn't in the rulebook at all -> all its PII-shaped
    # columns are flagged as undeclared_model, and the model is listed once.
    ticket_findings = [f for f in report.findings if f.table == "x_custom_ticket"]
    assert len(ticket_findings) == 4  # subject, customer_id, internal_note, payload
    assert all(f.shape == "undeclared_model" for f in ticket_findings)
    assert "x.custom.ticket" in report.undeclared_models


def test_analyze_flags_uncovered_columns_on_declared_model(rulebook, snapshot):
    # crm.lead is declared, but .name and .partner_id aren't covered.
    report = analyze(rulebook, snapshot)
    lead = {f.column: f for f in report.findings if f.table == "crm_lead"}
    assert "name" in lead and lead["name"].shape == "free_text"
    assert "partner_id" in lead and lead["partner_id"].shape == "partner_ref"
    # crm_lead.email_from / .phone / .description ARE covered -> not flagged.
    assert "email_from" not in lead
    assert "phone" not in lead
    assert "description" not in lead


def test_analyze_does_not_flag_covered_res_partner(rulebook, snapshot):
    report = analyze(rulebook, snapshot)
    partner_findings = [f for f in report.findings if f.table == "res_partner"]
    # res.partner is fully covered by 10_core.yml in the real rulebook.
    assert partner_findings == []


def test_analyze_counts_considered_and_covered(rulebook, snapshot):
    report = analyze(rulebook, snapshot)
    assert report.considered_columns > 0
    assert report.covered_columns > 0
    # considered = covered + flagged + (unflaggable "other" columns like
    # serial IDs / numerics / non-partner FKs). covered + flagged can't exceed
    # considered.
    assert report.covered_columns + len(report.findings) <= report.considered_columns
    # And the remainder is exactly the "other"-shaped columns.
    other = report.considered_columns - report.covered_columns - len(report.findings)
    assert other >= 0


def test_analyze_has_findings_true_when_gaps_exist(rulebook, snapshot):
    report = analyze(rulebook, snapshot)
    assert report.has_findings is True


def test_analyze_clean_when_everything_covered(rulebook):
    # A snapshot where every PII-shaped column is covered -> no findings.
    snap = SchemaSnapshot(source="test", tables={
        "res_partner": {
            "name": ColumnInfo("name", "character varying"),
            "email": ColumnInfo("email", "character varying"),
            "phone": ColumnInfo("phone", "character varying"),
            "street": ColumnInfo("street", "text"),
            "vat": ColumnInfo("vat", "character varying"),
            "comment": ColumnInfo("comment", "text"),
            "image_1920": ColumnInfo("image_1920", "bytea"),
            "partner_latitude": ColumnInfo("partner_latitude", "numeric"),
        },
    })
    report = analyze(rulebook, snap)
    assert report.findings == []
    assert report.has_findings is False


def test_analyze_ignore_models_suppresses(rulebook, snapshot):
    report = analyze(rulebook, snapshot, ignore_models={"x.custom.ticket"})
    assert all(f.table != "x_custom_ticket" for f in report.findings)
    assert "x.custom.ticket" not in report.undeclared_models


# ---------------------------------------------------------------------------
# round-trip: schema.json serialize -> load -> same findings
# ---------------------------------------------------------------------------


def test_schema_snapshot_roundtrips_json(snapshot):
    from odoo_synth.core.schema import load_snapshot
    import json, tempfile, os
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "schema.json")
        with open(p, "w") as f:
            f.write(snapshot.to_json())
        loaded = load_snapshot(p)
    assert set(loaded.tables) == set(snapshot.tables)
    assert loaded.tables["crm_lead"]["partner_id"].fk_target == "res_partner.id"

# ---------------------------------------------------------------------------
# column-name pattern rules (05_patterns.yml backstop)
# ---------------------------------------------------------------------------

_DUMP_WITH_CACHES = """
CREATE TABLE account_move (
    id serial PRIMARY KEY,
    name character varying,
    ref text,
    narration text,
    invoice_partner_display_name character varying,
    invoice_source_email character varying
);

CREATE TABLE ir_sequence (
    id serial PRIMARY KEY,
    name character varying NOT NULL,
    code character varying,
    prefix character varying
);

CREATE TABLE x_random_model (
    id serial PRIMARY KEY,
    some_display_name character varying,
    complete_name character varying,
    invoice_partner_name character varying,
    display_name character varying,
    integer_display_name integer
);
"""


def test_pattern_covers_display_name_caches_without_flagging():
    rb = load_and_validate("rules/")
    snap = snapshot_from_dump_sql(_DUMP_WITH_CACHES)
    report = analyze(rb, snap)
    flagged_cols = {f.column for f in report.findings
                    if f.table == "x_random_model"}
    assert "some_display_name" not in flagged_cols
    assert "complete_name" not in flagged_cols
    assert "invoice_partner_name" not in flagged_cols
    assert "display_name" not in flagged_cols
    # integer_display_name is shape=other -> pattern shape filter excludes it
    assert "integer_display_name" not in flagged_cols
    pat_cols = {f.table + "." + f.column for f in report.pattern_matches}
    assert "x_random_model.some_display_name" in pat_cols
    assert "x_random_model.complete_name" in pat_cols
    assert "x_random_model.invoice_partner_name" in pat_cols
    assert report.covered_by_pattern >= 4


def test_explicit_field_rule_wins_over_pattern():
    rb = load_and_validate("rules/")
    snap = snapshot_from_dump_sql(_DUMP_WITH_CACHES)
    report = analyze(rb, snap)
    am_pat = [f for f in report.pattern_matches if f.table == "account_move"]
    am_pat_cols = {f.column for f in am_pat}
    assert "invoice_partner_display_name" not in am_pat_cols
    assert "invoice_source_email" not in am_pat_cols
    assert "name" not in am_pat_cols
    assert "ref" not in am_pat_cols
    assert "narration" not in am_pat_cols


def test_pattern_validation_rejects_non_label_strategy():
    import tempfile, textwrap, os
    from odoo_synth.core.rulebook import RulebookError, load_rules, validate
    with tempfile.TemporaryDirectory() as d:
        open(os.path.join(d, "00_strategies.yml"), "w").write(textwrap.dedent('''\
            strategies:
              keep: { sql_template: null }
              redact_freetext:
                sql_template: "MASKED WITH FUNCTION odoo_synth.redact_text({column})"
        '''))
        open(os.path.join(d, "05_patterns.yml"), "w").write(textwrap.dedent('''\
            column_patterns:
              - match: '_display_name$'
                strategy: keep
                shapes: [free_text]
        '''))
        rb = load_rules(d)
        with pytest.raises(RulebookError):
            validate(rb)


def test_pattern_validation_rejects_bad_regex():
    import tempfile, textwrap, os
    from odoo_synth.core.rulebook import RulebookError, load_rules, validate
    with tempfile.TemporaryDirectory() as d:
        open(os.path.join(d, "00_strategies.yml"), "w").write(textwrap.dedent('''\
            strategies:
              redact_freetext:
                sql_template: "MASKED WITH FUNCTION odoo_synth.redact_text({column})"
        '''))
        open(os.path.join(d, "05_patterns.yml"), "w").write(textwrap.dedent('''\
            column_patterns:
              - match: '[unclosed'
                strategy: redact_freetext
                shapes: [free_text]
        '''))
        rb = load_rules(d)
        with pytest.raises(RulebookError):
            validate(rb)


def test_validate_directory_reports_pattern_count():
    from odoo_synth.core.rulebook import validate_directory
    s = validate_directory("rules/")
    assert s["column_patterns"] >= 5
