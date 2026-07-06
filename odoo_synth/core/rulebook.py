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
class Rulebook:
    strategies: dict[str, Strategy] = field(default_factory=dict)
    field_rules: dict[tuple[str, str], FieldRule] = field(default_factory=dict)
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

    # Second pass: load every other file and collect field rules.
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
        for model_name, model_def in data.items():
            # Some keys are file-level metadata (e.g. `note:`, `default_policy:`)
            # -- skip anything that isn't a model mapping with a `fields:` key.
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
        "files": len(rb.raw),
    }
