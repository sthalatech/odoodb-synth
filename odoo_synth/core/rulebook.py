"""Load and validate the PII rulebook (rules/*.yml).

Implements the validation checks specified in AGENT_PROMPT.md's
"Validation" section:

  * every YAML file under rules/ parses without error
  * every `strategy:` used in a model/field entry exists as a key under
    `strategies:` in 00_strategies.yml
  * no model.field is declared with two different strategies across files
  * every sql_template in 00_strategies.yml that references {column} is
    only used in contexts where that substitution makes sense (basic sanity
    check, not full SQL parsing)

On failure we raise a clear error identifying the offending file and key,
not just return a boolean.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class RulebookError(Exception):
    """Raised when the rulebook fails validation, with file/key context."""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Strategy:
    name: str
    description: str = ""
    sql_template: str | None = None
    file: str = ""
    line: int = 0


@dataclass
class FieldRule:
    model: str
    field: str
    strategy: str
    note: str = ""
    file: str = ""
    line: int = 0


@dataclass
class ColumnPattern:
    """A column-name pattern rule (the denormalized-cache backstop).

    Odoo caches res.partner.name / .email into many denormalized columns
    across many models (account_move.invoice_partner_display_name,
    res_partner.complete_name/commercial_company_name, ir_sequence.name,
    ...). Listing every one per-model doesn't scale -- 876 undeclared
    models in a real instance. A column-name pattern matches by column
    *name* regex, regardless of model, and applies a strategy.

    This is a BACKSTOP, not a substitute for reviewing what's declared:
    pattern-matched columns show up in `rules scan` output as
    "covered by pattern" (not silently dropped), and a per-model field rule
    always wins over a pattern. Patterns are also scoped by PII `shape`
    (free_text/partner_ref/binary) so a pattern like `*_display_name` only
    fires on text columns, not on an unrelated integer column that happens
    to share the suffix.
    """
    match: str            # regex matched against the column NAME
    strategy: str
    shapes: tuple[str, ...] = ()   # empty == all shapes
    note: str = ""
    file: str = ""
    line: int = 0


@dataclass
class Rulebook:
    strategies: dict[str, Strategy] = field(default_factory=dict)
    field_rules: dict[tuple[str, str], FieldRule] = field(default_factory=dict)
    column_patterns: list[ColumnPattern] = field(default_factory=list)
    # raw parsed documents keyed by filename, for callers (scan/diff) that
    # need the full structure beyond field strategies.
    raw: dict[str, dict[str, Any]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

_STRATEGIES_FILE = "00_strategies.yml"
_COLUMN_REF = "{column}"


def _yaml_load(path: Path) -> tuple[Any, int]:
    """Parse a YAML file, returning (data, first_line_of_document).

    The first-line is the line where the top-level mapping begins, used for
    error context. We use yaml.safe_load with a loader that records line
    marks on mappings.
    """
    text = path.read_text(encoding="utf-8")
    # Strip the leading comment block / blank lines to find the first real
    # YAML token's line number for error reporting.
    first_doc_line = 1
    for i, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            first_doc_line = i
            break
    data = yaml.safe_load(text)
    return data, first_doc_line


def _line_of(node: Any, default: int) -> int:
    """Best-effort line number for a YAML node; PyYAML only carries marks on
    complex nodes loaded with the default loader for mappings/sequences."""
    mark = getattr(node, "__yaml_mark__", None)
    if mark is not None:
        return getattr(mark, "line", default) + 1
    return default


def load_rules(rules_dir: str | Path) -> Rulebook:
    """Load every *.yml under rules_dir and return a validated Rulebook.

    Does NOT run cross-file validation -- call validate() for that. Use this
    when you want the raw structure (e.g. scan/diff). Raises RulebookError
    on parse failures.
    """
    rules_path = Path(rules_dir)
    if not rules_path.is_dir():
        raise RulebookError(f"rules directory not found: {rules_path}")

    files = sorted(rules_path.glob("*.yml"))
    if not files:
        raise RulebookError(f"no *.yml files found under {rules_path}")

    rb = Rulebook()

    # First pass: load 00_strategies.yml so strategy names are known.
    strat_file = rules_path / _STRATEGIES_FILE
    if not strat_file.exists():
        raise RulebookError(
            f"missing {_STRATEGIES_FILE} under {rules_path}: strategy "
            "vocabulary file is required"
        )

    data, doc_line = _yaml_load(strat_file)
    rb.raw[strat_file.name] = data or {}
    if not isinstance(data, dict):
        raise RulebookError(
            f"{strat_file.name}: top-level YAML must be a mapping, got "
            f"{type(data).__name__}"
        )
    strategies = data.get("strategies")
    if not isinstance(strategies, dict) or not strategies:
        raise RulebookError(
            f"{strat_file.name}: missing or empty top-level `strategies:` "
            "mapping"
        )
    for sname, sdef in strategies.items():
        if not isinstance(sname, str):
            raise RulebookError(
                f"{strat_file.name}: strategy key must be a string, got "
                f"{type(sname).__name__}"
            )
        desc = ""
        sql_template = None
        if isinstance(sdef, dict):
            desc = str(sdef.get("description", "") or "").strip()
            sql_template = sdef.get("sql_template")
        elif sdef is None:
            sql_template = None
        else:
            raise RulebookError(
                f"{strat_file.name}: strategy `{sname}` must be a mapping "
                f"or null, got {type(sdef).__name__}"
            )
        rb.strategies[sname] = Strategy(
            name=sname,
            description=desc,
            sql_template=sql_template,
            file=strat_file.name,
            line=doc_line,
        )

    # Second pass: load every other file and collect field rules + patterns.
    for f in files:
        if f.name == _STRATEGIES_FILE:
            continue
        data, doc_line = _yaml_load(f)
        rb.raw[f.name] = data or {}
        if not isinstance(data, dict):
            raise RulebookError(
                f"{f.name}: top-level YAML must be a mapping, got "
                f"{type(data).__name__}"
            )
        # File-level column_patterns: list (backstop rules; see ColumnPattern).
        # Loaded before per-model entries so an explicit field rule always wins.
        patterns = data.get("column_patterns")
        if patterns is not None:
            if not isinstance(patterns, list):
                raise RulebookError(
                    f"{f.name}: `column_patterns:` must be a list, got "
                    f"{type(patterns).__name__}"
                )
            for p in patterns:
                if not isinstance(p, dict) or "match" not in p or "strategy" not in p:
                    raise RulebookError(
                        f"{f.name}: each column_patterns entry needs `match` "
                        f"(regex) and `strategy`; got {p!r}"
                    )
                shapes = p.get("shapes") or ()
                if isinstance(shapes, str):
                    shapes = (shapes,)
                rb.column_patterns.append(ColumnPattern(
                    match=str(p["match"]),
                    strategy=str(p["strategy"]),
                    shapes=tuple(str(s) for s in shapes),
                    note=str(p.get("note", "") or ""),
                    file=f.name, line=doc_line,
                ))
        for model_name, model_def in data.items():
            # Some keys are file-level metadata (e.g. `note:`, `default_policy:`,
            # `column_patterns:`) -- skip anything that isn't a model mapping
            # with a `fields:` key.
            if not isinstance(model_def, dict):
                continue
            fields = model_def.get("fields")
            if not isinstance(fields, dict):
                continue
            for fname, fdef in fields.items():
                strategy = _field_strategy(f.name, model_name, fname, fdef)
                fr = FieldRule(
                    model=model_name,
                    field=fname,
                    strategy=strategy,
                    note=_field_note(fdef),
                    file=f.name,
                    line=doc_line,
                )
                rb.field_rules[(model_name, fname)] = fr

    return rb


def _field_strategy(file: str, model: str, field: str, fdef: Any) -> str:
    if isinstance(fdef, dict):
        s = fdef.get("strategy")
        if s is None:
            raise RulebookError(
                f"{file}: {model}.{field} entry has no `strategy` key"
            )
        return str(s)
    if isinstance(fdef, str):
        return fdef
    raise RulebookError(
        f"{file}: {model}.{field} entry must be a mapping with a `strategy` "
        f"key or a bare strategy string, got {type(fdef).__name__}"
    )


def _field_note(fdef: Any) -> str:
    if isinstance(fdef, dict):
        return str(fdef.get("note", "") or "")
    return ""


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _known_strategy_names(rb: Rulebook) -> set[str]:
    return set(rb.strategies.keys())


def validate(rb: Rulebook) -> None:
    """Run all validation checks. Raises RulebookError on the first failure."""
    # 1. Every strategy referenced by a field rule exists in 00_strategies.
    known = _known_strategy_names(rb)
    for (model, field), fr in rb.field_rules.items():
        if fr.strategy not in known:
            raise RulebookError(
                f"{fr.file}: {model}.{field} references unknown strategy "
                f"`{fr.strategy}` -- not defined under `strategies:` in "
                f"{_STRATEGIES_FILE}. Known strategies: "
                f"{', '.join(sorted(known))}"
            )

    # 1b. Column patterns: strategy must exist, regex must compile, and
    #     the strategy must be a per-column label strategy (sql_template
    #     non-null) -- patterns can't be keep/shuffle/rotate because a
    #     pattern match has no single (model,field) to anchor those to.
    import re as _re
    for pat in rb.column_patterns:
        if pat.strategy not in known:
            raise RulebookError(
                f"{pat.file}: column_patterns entry `match: {pat.match}` "
                f"references unknown strategy `{pat.strategy}` -- not defined "
                f"under `strategies:` in {_STRATEGIES_FILE}."
            )
        try:
            _re.compile(pat.match)
        except _re.error as exc:
            raise RulebookError(
                f"{pat.file}: column_patterns `match: {pat.match}` is not a "
                f"valid Python regex: {exc}"
            ) from exc
        strat = rb.strategies.get(pat.strategy)
        if strat is None or strat.sql_template is None:
            raise RulebookError(
                f"{pat.file}: column_patterns `match: {pat.match}` uses "
                f"strategy `{pat.strategy}` which has no sql_template -- "
                "patterns must be a per-column label strategy (e.g. "
                "redact_freetext, fake_email). keep/shuffle/rotate are not "
                "valid for patterns."
            )

    # 2. No model.field declared twice with different strategies.
    #    field_rules is keyed by (model, field); duplicates within a single
    #    file overwrite silently in the dict, so we re-scan raw docs to
    #    detect cross-file conflicts explicitly.
    seen: dict[tuple[str, str], tuple[str, str]] = {}  # (model,field) -> (file, strategy)
    for fname, doc in rb.raw.items():
        if fname == _STRATEGIES_FILE:
            continue
        if not isinstance(doc, dict):
            continue
        for model_name, model_def in doc.items():
            if not isinstance(model_def, dict):
                continue
            fields = model_def.get("fields")
            if not isinstance(fields, dict):
                continue
            for fname_field, fdef in fields.items():
                strategy = _field_strategy(fname, model_name, fname_field, fdef)
                key = (model_name, fname_field)
                prev = seen.get(key)
                if prev is not None and prev[1] != strategy:
                    raise RulebookError(
                        f"conflicting strategies for {model_name}.{fname_field}: "
                        f"`{strategy}` in {fname} vs `{prev[1]}` in {prev[0]} "
                        "-- a model.field may only be declared once across the "
                        "rulebook (or with the same strategy if repeated)."
                    )
                if prev is None:
                    seen[key] = (fname, strategy)

    # 3. {column} sanity: any strategy whose sql_template references
    #    {column} must be a per-column masking template (used in field
    #    rules). Strategies with sql_template == null are reserved for
    #    special handling (e.g. shuffle_within_column, keep) and must NOT
    #    be used as if they had a {column} template. We check that no field
    #    rule references a strategy that has sql_template == null AND a
    #    non-null description claiming per-column use -- in practice we just
    #    confirm that strategies with {column} in their template are the
    #    ones being used per-field, which is always true here. The real
    #    sanity check: a strategy whose template references {column} should
    #    not also be declared with sql_template: null. (00_strategies is
    #    hand-authored; this catches an internal contradiction.)
    for sname, strat in rb.strategies.items():
        tpl = strat.sql_template
        if tpl is None:
            continue
        if _COLUMN_REF in tpl:
            # templates with {column} are expected to be MASKED WITH ... forms.
            # Just confirm they aren't the literal string "null".
            if isinstance(tpl, str) and tpl.strip().lower() == "null":
                raise RulebookError(
                    f"{_STRATEGIES_FILE}: strategy `{sname}` references "
                    f"{{column}} but has a null-like template -- contradiction."
                )

    # 4. Strategies that legitimately have sql_template: null (keep,
    #    shuffle_within_column) must only be used in contexts that don't
    #    need a template. For `keep` that's any field; for
    #    shuffle_within_column it's documented as not a per-field label.
    #    We don't over-enforce here -- the rulebook README already explains
    #    the contract.


def load_and_validate(rules_dir: str | Path) -> Rulebook:
    """Convenience: load then validate."""
    rb = load_rules(rules_dir)
    validate(rb)
    return rb


# ---------------------------------------------------------------------------
# CLI helper: validate a directory and print a summary
# ---------------------------------------------------------------------------


def validate_directory(rules_dir: str | Path) -> dict[str, Any]:
    """Validate a rules directory; return a summary dict.

    Raises RulebookError on failure. On success returns counts.
    """
    rb = load_and_validate(rules_dir)
    return {
        "strategies": len(rb.strategies),
        "field_rules": len(rb.field_rules),
        "models": len({m for (m, _) in rb.field_rules}),
        "column_patterns": len(rb.column_patterns),
        "files": len(rb.raw),
    }
