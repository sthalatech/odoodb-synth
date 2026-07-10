"""Unit tests for provenance detection (DB-free, pure logic).

These cover the filesystem and reconciliation logic that doesn't need a live
Postgres: addons_path resolution/parsing, manifest reading, installed-vs-
available reconciliation, the git/runtime helpers' pure parts, and JSON
round-tripping. The DB layer (_detect_from_db) is exercised by the
integration suite against a real Postgres.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from odoo_synth.core import provenance
from odoo_synth.core.provenance import (
    AddonRepo,
    ModuleInfo,
    ProvenanceReport,
    content_hash_of_dir,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_addon(base: Path, name: str, version: str | None = "19.0.1.0") -> Path:
    d = base / name
    d.mkdir(parents=True)
    manifest = "{'name': '%s'%s}" % (
        name,
        f", 'version': '{version}'" if version is not None else "",
    )
    (d / "__manifest__.py").write_text(manifest, "utf-8")
    (d / "__init__.py").write_text("", "utf-8")
    return d


# ---------------------------------------------------------------------------
# manifest reading
# ---------------------------------------------------------------------------


def test_read_manifest_parses_version(tmp_path: Path) -> None:
    addon = _make_addon(tmp_path, "my_module", "19.0.2.5")
    manifest = provenance._read_manifest(addon)
    assert manifest is not None
    assert manifest["version"] == "19.0.2.5"


def test_read_manifest_legacy_openerp(tmp_path: Path) -> None:
    d = tmp_path / "old_mod"
    d.mkdir()
    (d / "__openerp__.py").write_text("{'name': 'old', 'version': '1.0'}", "utf-8")
    manifest = provenance._read_manifest(d)
    assert manifest is not None
    assert manifest["version"] == "1.0"


def test_read_manifest_none_for_non_addon(tmp_path: Path) -> None:
    d = tmp_path / "not_an_addon"
    d.mkdir()
    (d / "readme.txt").write_text("hi", "utf-8")
    assert provenance._read_manifest(d) is None


def test_read_manifest_unparseable_is_still_addon(tmp_path: Path) -> None:
    d = tmp_path / "broken"
    d.mkdir()
    (d / "__manifest__.py").write_text("{ this is not valid python", "utf-8")
    # Present but unparseable -> empty dict (still recognized as an addon dir).
    assert provenance._read_manifest(d) == {}


def test_read_manifest_never_executes_code(tmp_path: Path) -> None:
    # A manifest with a side-effecting expression must NOT run (ast.literal_eval).
    d = tmp_path / "evil"
    d.mkdir()
    sentinel = tmp_path / "SHOULD_NOT_EXIST"
    (d / "__manifest__.py").write_text(
        f"__import__('pathlib').Path({str(sentinel)!r}).write_text('x')", "utf-8"
    )
    result = provenance._read_manifest(d)
    assert result == {}  # literal_eval refuses a call expression
    assert not sentinel.exists()


# ---------------------------------------------------------------------------
# addons_path resolution
# ---------------------------------------------------------------------------


def test_resolve_addons_path_explicit_and_dedup(tmp_path: Path) -> None:
    a = tmp_path / "a"
    a.mkdir()
    report = ProvenanceReport()
    resolved = provenance._resolve_addons_path(
        None, [str(a), str(a)], report
    )
    assert resolved == [str(a)]  # de-duplicated


def test_resolve_addons_path_drops_missing(tmp_path: Path) -> None:
    a = tmp_path / "exists"
    a.mkdir()
    report = ProvenanceReport()
    resolved = provenance._resolve_addons_path(
        None, [str(a), str(tmp_path / "nope")], report
    )
    assert resolved == [str(a)]
    assert any("does not exist" in w for w in report.warnings)


def test_parse_addons_path_from_conf(tmp_path: Path) -> None:
    a = tmp_path / "addons1"
    b = tmp_path / "addons2"
    a.mkdir()
    b.mkdir()
    conf = tmp_path / "odoo.conf"
    conf.write_text(
        f"[options]\naddons_path = {a},{b}\ndb_name = x\n", "utf-8"
    )
    report = ProvenanceReport()
    resolved = provenance._resolve_addons_path(str(conf), None, report)
    assert resolved == [str(a), str(b)]


def test_parse_addons_path_missing_conf(tmp_path: Path) -> None:
    report = ProvenanceReport()
    resolved = provenance._resolve_addons_path(
        str(tmp_path / "nope.conf"), None, report
    )
    assert resolved == []
    assert any("not found" in w for w in report.warnings)


def test_parse_addons_path_conf_without_option(tmp_path: Path) -> None:
    conf = tmp_path / "odoo.conf"
    conf.write_text("[options]\ndb_name = x\n", "utf-8")
    report = ProvenanceReport()
    resolved = provenance._resolve_addons_path(str(conf), None, report)
    assert resolved == []
    assert any("no addons_path" in w for w in report.warnings)


# ---------------------------------------------------------------------------
# reconciliation: installed modules vs addon code
# ---------------------------------------------------------------------------


def test_detect_addons_reconciles_and_flags_missing(tmp_path: Path) -> None:
    addons = tmp_path / "addons"
    addons.mkdir()
    _make_addon(addons, "sale", "19.0.1.4")
    _make_addon(addons, "stock", "19.0.1.0")
    # 'custom_mod' is installed but has NO code on disk.

    report = ProvenanceReport()
    report.installed_modules = [
        ModuleInfo("sale", "19.0.1.4"),
        ModuleInfo("stock", "19.0.1.0"),
        ModuleInfo("custom_mod", "19.0.1.0"),
    ]
    provenance._detect_addons([str(addons)], report)

    by_name = {m.name: m for m in report.installed_modules}
    assert by_name["sale"].code_path == str(addons / "sale")
    assert by_name["sale"].manifest_version == "19.0.1.4"
    assert by_name["custom_mod"].code_path is None
    assert report.missing_addons == ["custom_mod"]
    assert any("cannot boot" in w for w in report.warnings)


def test_detect_addons_precedence_left_wins(tmp_path: Path) -> None:
    # Same addon in two paths: the left-most addons_path entry wins.
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    _make_addon(first, "dup", "1.0")
    _make_addon(second, "dup", "2.0")

    report = ProvenanceReport()
    report.installed_modules = [ModuleInfo("dup", "1.0")]
    provenance._detect_addons([str(first), str(second)], report)

    mod = report.installed_modules[0]
    assert mod.code_path == str(first / "dup")
    assert mod.manifest_version == "1.0"
    assert report.missing_addons == []


def test_detect_addons_all_resolved_no_warning(tmp_path: Path) -> None:
    addons = tmp_path / "addons"
    addons.mkdir()
    _make_addon(addons, "base", "19.0.1.3")
    report = ProvenanceReport()
    report.installed_modules = [ModuleInfo("base", "19.0.1.3")]
    provenance._detect_addons([str(addons)], report)
    assert report.missing_addons == []


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "version,expected",
    [("16.14", 16), ("18.4 (Ubuntu)", 18), ("9.6", 9), ("", 0), ("beta", 0)],
)
def test_major_int(version: str, expected: int) -> None:
    assert provenance._major_int(version) == expected


@pytest.mark.parametrize(
    "base,expected",
    [
        ("19.0.1.3", "19.0"),
        ("18.0.1.4", "18.0"),
        ("saas~17.2", "saas~17.2"),  # non-numeric first segment -> raw
        ("", ""),
    ],
)
def test_odoo_series(base: str, expected: str) -> None:
    assert provenance._odoo_series(base) == expected


# ---------------------------------------------------------------------------
# content hashing
# ---------------------------------------------------------------------------


def test_content_hash_stable_and_ignores_pycache(tmp_path: Path) -> None:
    addon = _make_addon(tmp_path, "mod")
    h1 = content_hash_of_dir(addon)
    # Adding a __pycache__ / .pyc must not change the hash.
    (addon / "__pycache__").mkdir()
    (addon / "__pycache__" / "x.cpython-312.pyc").write_bytes(b"\x00\x01")
    h2 = content_hash_of_dir(addon)
    assert h1 == h2


def test_content_hash_changes_with_source(tmp_path: Path) -> None:
    addon = _make_addon(tmp_path, "mod")
    h1 = content_hash_of_dir(addon)
    (addon / "models.py").write_text("x = 1", "utf-8")
    h2 = content_hash_of_dir(addon)
    assert h1 != h2


# ---------------------------------------------------------------------------
# JSON round-trip
# ---------------------------------------------------------------------------


def test_report_json_round_trip() -> None:
    rep = ProvenanceReport(
        source_db_name="isha",
        postgres_version="16.14",
        postgres_major=16,
        db_encoding="UTF8",
        odoo_series="19.0",
        odoo_base_version="19.0.1.3",
        installed_module_count=2,
        installed_modules=[
            ModuleInfo("base", "19.0.1.3", "/a/base", "19.0.1.3"),
            ModuleInfo("sale", "19.0.1.4"),
        ],
        addons_path=["/a"],
        addon_repos=[AddonRepo("/a", "git@x", "abc123", False, 2)],
        missing_addons=["sale"],
        odoo_bin_version="Odoo Server 19.0",
        python_version="Python 3.12.3",
        warnings=["w1"],
    )
    restored = ProvenanceReport.from_dict(
        __import__("json").loads(rep.to_json())
    )
    assert restored.to_dict() == rep.to_dict()
    assert restored.installed_modules[0].code_path == "/a/base"
    assert restored.addon_repos[0].git_commit == "abc123"
