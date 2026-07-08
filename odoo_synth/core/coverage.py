"""Rulebook coverage analysis -- the engine behind `rules scan` / `rules diff`.

Per rules/README.md, `rules scan` flags new Char/Text/Many2one(res.partner)
fields on any installed model that aren't yet declared `keep` or given a
strategy. This module is the engine: given a rulebook and a schema snapshot,
it classifies every column by PII shape and reports the ones the rulebook
doesn't cover.

The classification is deliberately conservative on the *flag* side (we'd
rather over-flag a column for human review than silently let a sensitive
field ship unmasked) and deliberately explicit about its heuristics so the
operator understands why a column was flagged.

Shapes (from rules/README.md's "Char/Text/Many2one(res.partner)" framing):

  * ``free_text``  -- character/varchar/text columns that can hold a
    human-typed string (names, notes, descriptions, addresses). These are
    the highest-value targets; the rulebook's redact_freetext / fake_*
    strategies exist for them.
  * ``partner_ref`` -- a FK into res_partner. Always PII-shaped because the
    partner row is the PII (the column is just a pointer to it).
  * ``identifier`` -- numeric/serial ID columns that aren't FKs into a PII
    table. NOT flagged by default (IDs are operational, not PII), but
    surfaced in the summary so the operator sees what was considered.
  * ``binary``     -- bytea columns (attachments, images). Flagged because
    the default attachment policy drops content but a per-model keep can
    leak; the operator should confirm the model is covered by
    50_attachments.yml.
  * ``other``      -- everything else (booleans, dates, numerics not IDs).
    Not flagged.

A column is "covered" if the rulebook has ANY field rule for (model, field)
-- including `keep`, which is the explicit "reviewed, non-sensitive" mark.
A column that maps to a model the rulebook doesn't mention at all is
flagged as an UNDECLARED MODEL (the whole model needs review), which is the
strongest signal and corresponds to odoo_sh.ingest()'s undeclared_modules
list.

The model<->table mapping follows Odoo's convention: the rulebook keys
models with dots (``res.partner``) and Postgres tables use underscores
(``res_partner``); ``ir.config_parameter`` -> ``ir_config_parameter`` is the
canonical example.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from .rulebook import Rulebook
from .schema import ColumnInfo, SchemaSnapshot


# Tables a partner_ref FK points into. The FK target's "model name" is the
# table with underscores; res_partner is the only one by default, but an
# instance with custom partner-like tables could extend this.
_PARTNER_TABLES = {"res_partner"}


@dataclass
class Finding:
    table: str
    column: str
    shape: str  # free_text | partner_ref | binary | undeclared_model
    data_type: str
    reason: str
    fk_target: str | None = None
    not_null: bool = False


@dataclass
class CoverageReport:
    findings: list[Finding] = field(default_factory=list)
    covered_columns: int = 0
    considered_columns: int = 0
    undeclared_models: list[str] = field(default_factory=list)
    unparsed_tables: list[str] = field(default_factory=list)

    @property
    def has_findings(self) -> bool:
        return bool(self.findings)

    def summary(self) -> str:
        lines = [
            f"considered {self.considered_columns} columns, "
            f"{self.covered_columns} covered by the rulebook, "
            f"{len(self.findings)} flagged.",
        ]
        if self.undeclared_models:
            lines.append(
                f"undeclared models (no rulebook entry at all): "
                f"{', '.join(sorted(self.undeclared_models))}"
            )
        if self.unparsed_tables:
            lines.append(
                f"WARNING: {len(self.unparsed_tables)} table(s) in the "
                "snapshot could not be parsed -- the scan is incomplete for "
                f"them: {', '.join(sorted(self.unparsed_tables))}"
            )
        return "\n".join(lines)


# Reverse table->model mapping needs the rulebook to disambiguate, because
# Odoo table names replace ALL dots with underscores but model names keep
# underscores in their last segment: `ir.config_parameter` -> table
# `ir_config_parameter`, which naive full-replacement would turn back into
# `ir.config.parameter` (wrong). We resolve by preferring a rulebook-declared
# model whose table form (dots->underscores) matches exactly; only fall back
# to naive replacement for models the rulebook doesn't declare.


def _model_to_table(model: str) -> str:
    """res.partner -> res_partner (Odoo table-name convention)."""
    return model.replace(".", "_")


def _build_table_model_index(rulebook: Rulebook) -> dict[str, str]:
    """table -> model name, for every model the rulebook declares."""
    return {_model_to_table(m): m for m in {mr for (mr, _) in rulebook.field_rules}}


def _table_to_model(table: str, index: dict[str, str]) -> str:
    """res_partner -> 'res.partner' if declared, else best-guess replacement.

    The rulebook index disambiguates multi-segment models (e.g.
    ir_config_parameter -> ir.config_parameter, not ir.config.parameter).
    For a table the rulebook doesn't declare (the undeclared-model case),
    we fall back to replacing ALL underscores with dots -- a heuristic that's
    only used to LABEL the undeclared model in the report, never to match a
    rule, so a wrong guess here is cosmetic, not a correctness issue.
    """
    if table in index:
        return index[table]
    return table.replace("_", ".")


def _model_to_table(model: str) -> str:
    """res.partner -> res_partner (Postgres table name)."""
    return model.replace(".", "_")


def classify_shape(col: ColumnInfo) -> str:
    """Map a column's type/fk to a PII shape name."""
    if col.fk_target:
        ref_tbl = col.fk_target.split(".")[0]
        if ref_tbl in _PARTNER_TABLES:
            return "partner_ref"
        # FK into a non-partner table is not auto-PII; it's structural.
        return "other"
    dtype = (col.data_type or "").lower()
    # Strip type modifiers for matching: "character varying(255)" -> base check.
    base = dtype.split("(")[0].strip()
    if base in {"text", "character varying", "varchar", "char", "character"}:
        return "free_text"
    if base in {"bytea"}:
        return "binary"
    return "other"


# Shapes that warrant a finding when uncovered. "other" (numeric IDs, dates,
# booleans, FKs into non-partner tables) is NOT flagged -- per rules/README.md
# the scan targets Char/Text/Many2one(res.partner).
_FLAGGABLE_SHAPES = {"free_text", "partner_ref", "binary"}


def analyze(rulebook: Rulebook, snapshot: SchemaSnapshot,
            ignore_models: Iterable[str] | None = None) -> CoverageReport:
    """Compare a rulebook against a schema snapshot.

    Returns a CoverageReport. ``ignore_models`` lets the caller suppress
    models known to be out of scope (e.g. audit/log tables that are dropped
    wholesale by policy); entries are Odoo model names (``res.partner``).
    """
    report = CoverageReport(unparsed_tables=list(snapshot.unparsed_tables))
    ignore = set(ignore_models or [])

    declared_models = {m for (m, _) in rulebook.field_rules}
    table_index = _build_table_model_index(rulebook)
    # Precompute covered (model, field) pairs.
    covered = set(rulebook.field_rules.keys())

    for table, cols in snapshot.tables.items():
        model = _table_to_model(table, table_index)
        if model in ignore:
            continue
        model_declared = model in declared_models
        for colname, ci in cols.items():
            report.considered_columns += 1
            shape = classify_shape(ci)
            if (model, colname) in covered:
                report.covered_columns += 1
                continue  # explicitly handled (incl. keep)
            if not model_declared:
                # The whole model is undeclared. Flag the PII-shaped columns
                # and record the model once for the undeclared-models summary.
                if shape in _FLAGGABLE_SHAPES:
                    report.findings.append(Finding(
                        table=table, column=colname, shape="undeclared_model",
                        data_type=ci.data_type,
                        reason=f"model `{model}` has no rulebook entry; "
                               f"column is {shape}",
                        fk_target=ci.fk_target, not_null=ci.not_null,
                    ))
                    if model not in report.undeclared_models:
                        report.undeclared_models.append(model)
                continue
            # Model is declared but this specific column isn't covered.
            if shape in _FLAGGABLE_SHAPES:
                report.findings.append(Finding(
                    table=table, column=colname, shape=shape,
                    data_type=ci.data_type,
                    reason=f"{model}.{colname} is a {shape} column "
                           f"({ci.data_type}) with no rulebook strategy "
                           "(declare it `keep` if reviewed, or add a "
                           "strategy)",
                    fk_target=ci.fk_target, not_null=ci.not_null,
                ))
    # Sort findings for stable output / CI diffs.
    report.findings.sort(key=lambda f: (f.table, f.column))
    report.undeclared_models.sort()
    return report
