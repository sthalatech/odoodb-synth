"""Layer 1 (TESTING.md section 1): rulebook validation.

No database needed. Runs on every PR. Checks that every YAML parses, every
strategy referenced by a model/field entry exists in 00_strategies.yml, and
no model.field is declared with conflicting strategies across files.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from odoo_synth.core.rulebook import RulebookError, load_and_validate, validate_directory

REPO_ROOT = Path(__file__).resolve().parents[2]
RULES_DIR = REPO_ROOT / "rules"


def test_rules_dir_exists():
    assert RULES_DIR.is_dir(), f"rules/ not found at {RULES_DIR}"


def test_validate_provided_rulebook_passes():
    """The committed rulebook must validate cleanly."""
    summary = validate_directory(RULES_DIR)
    assert summary["strategies"] >= 8, "expected at least 8 strategies in 00_strategies.yml"
    assert summary["field_rules"] > 0, "expected field rules across 10_..70_*.yml"
    # 00_strategies.yml + 7 numbered module files (10..70).
    assert summary["files"] >= 8, "expected 00_strategies + 7 module files"


def test_all_referenced_strategies_exist():
    """Every strategy: used in 10_..70_*.yml is defined in 00_strategies.yml."""
    rb = load_and_validate(RULES_DIR)
    known = set(rb.strategies.keys())
    for (_model, _field), fr in rb.field_rules.items():
        assert fr.strategy in known, (
            f"{fr.file}: {fr.model}.{fr.field} uses unknown strategy {fr.strategy!r}"
        )


def test_no_conflicting_strategies_across_files():
    """No model.field declared twice with different strategies."""
    # load_and_validate already runs this check; if it passes, we're good.
    # Re-run the raw scan to assert explicitly.
    rb = load_and_validate(RULES_DIR)
    seen: dict[tuple[str, str], tuple[str, str]] = {}
    for fname, doc in rb.raw.items():
        if fname == "00_strategies.yml" or not isinstance(doc, dict):
            continue
        for model_name, model_def in doc.items():
            if not isinstance(model_def, dict):
                continue
            fields = model_def.get("fields")
            if not isinstance(fields, dict):
                continue
            for fname_field, fdef in fields.items():
                strategy = (
                    fdef.get("strategy") if isinstance(fdef, dict) else fdef
                )
                key = (model_name, fname_field)
                prev = seen.get(key)
                if prev is not None:
                    assert prev[1] == strategy, (
                        f"conflict on {model_name}.{fname_field}: "
                        f"{strategy!r} in {fname} vs {prev[1]!r} in {prev[0]}"
                    )
                else:
                    seen[key] = (fname, strategy)


def test_unknown_strategy_is_caught(tmp_path):
    """A typo'd strategy name must produce a clear RulebookError."""
    strat = tmp_path / "00_strategies.yml"
    strat.write_text(
        "strategies:\n  drop:\n    sql_template: null\n  keep:\n    sql_template: null\n",
        encoding="utf-8",
    )
    bad = tmp_path / "10_bad.yml"
    bad.write_text(
        "res.partner:\n  fields:\n    name: { strategy: typo_strategy }\n",
        encoding="utf-8",
    )
    with pytest.raises(RulebookError) as exc:
        load_and_validate(tmp_path)
    assert "typo_strategy" in str(exc.value)
    assert "10_bad.yml" in str(exc.value)


def test_conflicting_strategies_are_caught(tmp_path):
    """Two files declaring the same model.field with different strategies must fail."""
    strat = tmp_path / "00_strategies.yml"
    strat.write_text(
        "strategies:\n  drop: { sql_template: null }\n  keep: { sql_template: null }\n",
        encoding="utf-8",
    )
    (tmp_path / "10_a.yml").write_text(
        "res.partner:\n  fields:\n    name: { strategy: drop }\n", encoding="utf-8"
    )
    (tmp_path / "20_b.yml").write_text(
        "res.partner:\n  fields:\n    name: { strategy: keep }\n", encoding="utf-8"
    )
    with pytest.raises(RulebookError) as exc:
        load_and_validate(tmp_path)
    msg = str(exc.value)
    assert "res.partner.name" in msg
    assert "conflict" in msg.lower()


def test_missing_strategies_file_is_caught(tmp_path):
    # Put a module file in the dir but no 00_strategies.yml, so we get past
    # the "no *.yml files" check and hit the missing-strategies-file check.
    (tmp_path / "10_core.yml").write_text(
        "res.partner:\n  fields:\n    name: { strategy: drop }\n", encoding="utf-8"
    )
    with pytest.raises(RulebookError) as exc:
        load_and_validate(tmp_path)
    assert "00_strategies.yml" in str(exc.value)
