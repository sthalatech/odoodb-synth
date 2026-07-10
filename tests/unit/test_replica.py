"""Unit tests for the replica-kit generator (DB-free, pure generation).

These verify that generate_kit() emits the expected files, that the generated
scripts embed the source provenance (PG major, Odoo series, installed module
list) and the target configuration, that the version-skew policy wording flips
with allow_mismatch, and that a bundle without provenance.json / db.dump fails
loudly. Nothing here connects to Postgres or executes the kit.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from odoo_synth.core import replica
from odoo_synth.core.replica import ReplicaConfig, ReplicaError, generate_kit
from odoo_synth.core.provenance import ModuleInfo, ProvenanceReport


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


def _make_report() -> ProvenanceReport:
    rep = ProvenanceReport(
        source_db_name="isha",
        postgres_version="16.14",
        postgres_major=16,
        db_encoding="UTF8",
        odoo_series="19.0",
        odoo_base_version="19.0.1.3",
        installed_module_count=3,
    )
    rep.installed_modules = [
        ModuleInfo(name="base", installed_version="19.0.1.3"),
        ModuleInfo(name="web", installed_version="19.0.1.0"),
        ModuleInfo(name="mail", installed_version="19.0.1.0"),
    ]
    return rep


def _make_bundle(tmp_path: Path, *, with_provenance: bool = True, with_dump: bool = True) -> Path:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    if with_provenance:
        (bundle / "provenance.json").write_text(_make_report().to_json(), "utf-8")
    if with_dump:
        (bundle / "db.dump").write_bytes(b"PGDMP fake")
    (bundle / "filestore").mkdir()
    return bundle


# ---------------------------------------------------------------------------
# happy path: files created
# ---------------------------------------------------------------------------


def test_generate_kit_creates_all_files(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    out = tmp_path / "kit"
    result = generate_kit(bundle, out, ReplicaConfig())
    assert result == out
    for name in [
        "preflight.sh",
        "install.sh",
        "odoo.conf",
        "odoo.service",
        "provenance.json",
        "README.md",
    ]:
        assert (out / name).is_file(), f"missing {name}"


def test_service_file_named_after_service(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    out = tmp_path / "kit"
    generate_kit(bundle, out, ReplicaConfig(service_name="odoo-replica"))
    assert (out / "odoo-replica.service").is_file()
    assert not (out / "odoo.service").is_file()


def test_scripts_are_executable(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    out = tmp_path / "kit"
    generate_kit(bundle, out, ReplicaConfig())
    for name in ["preflight.sh", "install.sh"]:
        mode = (out / name).stat().st_mode
        assert mode & stat.S_IXUSR, f"{name} not executable"


def test_provenance_copied_verbatim(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    out = tmp_path / "kit"
    generate_kit(bundle, out, ReplicaConfig())
    src = json.loads((bundle / "provenance.json").read_text())
    dst = json.loads((out / "provenance.json").read_text())
    assert src == dst


# ---------------------------------------------------------------------------
# preflight embeds provenance facts
# ---------------------------------------------------------------------------


def test_preflight_embeds_pg_major_and_series(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    out = tmp_path / "kit"
    generate_kit(bundle, out, ReplicaConfig())
    text = (out / "preflight.sh").read_text()
    assert "EXPECTED_PG_MAJOR=16" in text
    assert "EXPECTED_ODOO_SERIES='19.0'" in text


def test_preflight_lists_installed_modules(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    out = tmp_path / "kit"
    generate_kit(bundle, out, ReplicaConfig())
    text = (out / "preflight.sh").read_text()
    assert "INSTALLED_MODULES='base web mail'" in text


def test_preflight_missing_module_is_always_fatal(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    out = tmp_path / "kit"
    # Even with allow_mismatch, missing addon code must call fail.
    generate_kit(bundle, out, ReplicaConfig(allow_mismatch=True))
    text = (out / "preflight.sh").read_text()
    assert 'fail "installed module code missing' in text


# ---------------------------------------------------------------------------
# version-skew policy wording
# ---------------------------------------------------------------------------


def test_mismatch_default_is_fail(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    out = tmp_path / "kit"
    generate_kit(bundle, out, ReplicaConfig(allow_mismatch=False))
    text = (out / "preflight.sh").read_text()
    assert "MISMATCH_ACTION='fail'" in text


def test_mismatch_allowed_is_warn(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    out = tmp_path / "kit"
    generate_kit(bundle, out, ReplicaConfig(allow_mismatch=True))
    text = (out / "preflight.sh").read_text()
    assert "MISMATCH_ACTION='warn'" in text


# ---------------------------------------------------------------------------
# install.sh content
# ---------------------------------------------------------------------------


def test_install_pins_search_path(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    out = tmp_path / "kit"
    generate_kit(bundle, out, ReplicaConfig())
    text = (out / "install.sh").read_text()
    assert "SET search_path TO public" in text


def test_install_uses_pg_restore_flags(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    out = tmp_path / "kit"
    generate_kit(bundle, out, ReplicaConfig())
    text = (out / "install.sh").read_text()
    assert "pg_restore --no-owner --no-privileges --clean --if-exists" in text


def test_install_verifies_res_partner(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    out = tmp_path / "kit"
    generate_kit(bundle, out, ReplicaConfig())
    text = (out / "install.sh").read_text()
    assert "to_regclass('public.res_partner')" in text


def test_install_embeds_neutralize_sql(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    out = tmp_path / "kit"
    generate_kit(bundle, out, ReplicaConfig())
    text = (out / "install.sh").read_text()
    assert "UPDATE payment_provider SET state = 'disabled';" in text
    assert "UPDATE ir_mail_server SET active = false" in text
    assert "UPDATE fetchmail_server SET active = false" in text


def test_install_uses_pbkdf2_hash(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    out = tmp_path / "kit"
    generate_kit(bundle, out, ReplicaConfig())
    text = (out / "install.sh").read_text()
    assert "pbkdf2_sha512" in text


def test_install_db_host_flag_omitted_when_empty(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    out = tmp_path / "kit"
    generate_kit(bundle, out, ReplicaConfig(db_host=""))
    text = (out / "install.sh").read_text()
    assert " -h " not in text


def test_install_db_host_flag_present_when_set(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    out = tmp_path / "kit"
    generate_kit(bundle, out, ReplicaConfig(db_host="db.internal"))
    text = (out / "install.sh").read_text()
    assert "-h 'db.internal'" in text


# ---------------------------------------------------------------------------
# odoo.conf + systemd rendering
# ---------------------------------------------------------------------------


def test_odoo_conf_renders_target_params(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    out = tmp_path / "kit"
    generate_kit(bundle, out, ReplicaConfig(
        addons_path="/srv/odoo/addons", http_port=8071, db_name="masked",
    ))
    text = (out / "odoo.conf").read_text()
    assert "addons_path = /srv/odoo/addons" in text
    assert "http_port = 8071" in text
    assert "dbfilter = ^masked$" in text
    assert "list_db = False" in text
    assert "proxy_mode = True" in text


def test_systemd_unit_renders_service_identity(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    out = tmp_path / "kit"
    generate_kit(bundle, out, ReplicaConfig(
        service_user="odoo-svc", python_bin="/usr/bin/python3.12",
        odoo_bin="/srv/odoo/odoo-bin", service_name="odoo-rep",
    ))
    text = (out / "odoo-rep.service").read_text()
    assert "User=odoo-svc" in text
    assert "ExecStart=/usr/bin/python3.12 /srv/odoo/odoo-bin -c /etc/odoo/odoo.conf" in text


def test_readme_mentions_series_and_policy(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    out = tmp_path / "kit"
    generate_kit(bundle, out, ReplicaConfig(allow_mismatch=True))
    text = (out / "README.md").read_text()
    assert "19.0" in text
    assert "warn (allowed)" in text


# ---------------------------------------------------------------------------
# error paths
# ---------------------------------------------------------------------------


def test_missing_provenance_raises(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path, with_provenance=False)
    out = tmp_path / "kit"
    with pytest.raises(ReplicaError, match="provenance.json"):
        generate_kit(bundle, out, ReplicaConfig())


def test_missing_dump_raises(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path, with_dump=False)
    out = tmp_path / "kit"
    with pytest.raises(ReplicaError, match="db.dump"):
        generate_kit(bundle, out, ReplicaConfig())


def test_bad_provenance_json_raises(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    (bundle / "provenance.json").write_text("{not valid json", "utf-8")
    out = tmp_path / "kit"
    with pytest.raises(ReplicaError, match="could not parse"):
        generate_kit(bundle, out, ReplicaConfig())


# ---------------------------------------------------------------------------
# sh-quoting safety
# ---------------------------------------------------------------------------


def test_sh_quote_escapes_single_quotes() -> None:
    assert replica._sh_quote("a'b") == "'a'\\''b'"
    assert replica._sh_quote("plain") == "'plain'"
