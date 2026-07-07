"""Layer 2 (TESTING.md section 2): SQL function unit tests.

Needs a Postgres with the `anon` extension and the odoo_synth bootstrap
functions loaded -- i.e. the `postgres-anon` service from
docker/docker-compose.scratch.yml. Auto-skipped if SCRATCH_DB_URL is unset
or unreachable, so the rulebook tests (layer 1) still run anywhere.

For each function in sql/bootstrap.sql, asserts:
  * Determinism: same input, same session -> identical output.
  * Non-identity: output != input.
  * Format validity: fake_vat preserves prefix+digit count; fake_iban
    passes a standard mod-97 IBAN checksum check (verified with the same
    mod-97 reference used to GENERATE the value, per TESTING.md -- so the
    test can't pass if generation and verification disagree).
  * NULL passthrough: NULL in -> NULL out, no exceptions.
"""

from __future__ import annotations

import pytest

# Sample inputs covering the formats we care about.
SAMPLE_NAMES = ["Alice Johnson", "Bob Smith", "Émile Faux-Nom", "X"]
SAMPLE_EMAILS = ["alice@acme.com", "bob@other.co.uk", "x@y.org"]
SAMPLE_VATS = ["BE0123456789", "GB123456789", "DE123456789", "FR12345678901"]
# Real, checksum-valid IBANs of varying country/length.
SAMPLE_IBANS = [
    "GB29NWBK60161331926819",   # GB, 22 chars
    "BE68539007547034",          # BE, 16 chars
    "DE89370400440532013000",    # DE, 22 chars
    "FR1420041010050500013M02606",  # FR, 27 chars (includes letters in BBAN)
]
SAMPLE_PHONES = ["+32 470 12 34 56", "555-1234", "0118 999 881 999 119 725 3"]
SAMPLE_TEXTS = ["short", "A longer free-text note with real PII inside: John's home address.", "x" * 200]


# ---------------------------------------------------------------------------
# mod-97 IBAN verification -- INDEPENDENT oracle
# ---------------------------------------------------------------------------
#
# IMPORTANT (from the phase-3 prompt): a test that verifies generated output
# against the *same* arithmetic the generation code uses is tautological --
# it can't catch a generation bug. The first version of this test hand-rolled
# the exact mod-97 procedure sql/bootstrap.sql::fake_iban uses, so a bug in
# either place would agree with itself.
#
# We instead use python-stdnum's stdnum.iso7064.mod_97_10, a third-party
# reference implementation of ISO 7064 mod-97-10. It is a separate code path
# from the PL/pgSQL generator, so a generation bug that produces a
# mod-97-invalid IBAN fails here. stdnum.iban.is_valid additionally runs
# per-country BBAN validators (UK sort-code structure, Dutch elfproef, etc.)
# which fake_iban deliberately does NOT satisfy -- that's the documented
# known limitation in sql/bootstrap.sql, and test_fake_iban_known_limitations
# asserts it explicitly rather than leaving it as a hidden gap.

try:
    from stdnum.iso7064 import mod_97_10 as _mod97
except ImportError:  # pragma: no cover
    _mod97 = None


def _iban_generic_mod97_ok(iban: str) -> bool:
    """Generic ISO 7064 mod-97 check via python-stdnum (independent oracle)."""
    if _mod97 is None:
        pytest.skip('python-stdnum not installed (dev extra); pip install -e \".[dev]\"')
    s = "".join(iban.upper().split())
    if len(s) < 5:
        return False
    rearr = s[4:] + s[:4]  # BBAN + country code + check digits
    try:
        return bool(_mod97.is_valid(rearr))
    except Exception:
        return False


def _iban_strict_country_ok(iban: str) -> bool:
    """python-stdnum's full per-country IBAN check (sort code / elfproef / ...)."""
    if _mod97 is None:
        pytest.skip("python-stdnum not installed (dev extra)")
    from stdnum import iban as _iban
    try:
        return bool(_iban.is_valid(iban))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _scalar(cur, expr: str, params=None):
    cur.execute(expr, params)
    row = cur.fetchone()
    return row[0] if row else None


def _det(cur, fn_call: str, value: str):
    """Call a single-arg odoo_synth function twice; assert identical output."""
    a = _scalar(cur, f"SELECT {fn_call}(%s)", (value,))
    b = _scalar(cur, f"SELECT {fn_call}(%s)", (value,))
    assert a == b, f"non-deterministic: {fn_call}({value!r}) -> {a!r} then {b!r}"
    return a


# ---------------------------------------------------------------------------
# determinism + non-identity + NULL passthrough
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", SAMPLE_NAMES)
def test_seeded_deterministic(cur, value):
    # seeded() takes (source_value, expr). Use the same expr shape the
    # rulebook's fake_person_name strategy uses.
    expr = "anon.fake_first_name() || ' ' || anon.fake_last_name()"
    a = _scalar(cur, "SELECT odoo_synth.seeded(%s, %s)", (value, expr))
    b = _scalar(cur, "SELECT odoo_synth.seeded(%s, %s)", (value, expr))
    assert a == b, f"non-deterministic: seeded({value!r}) -> {a!r} then {b!r}"
    assert a is not None
    assert a != value


def test_seeded_null_passthrough(cur):
    assert _scalar(cur, "SELECT odoo_synth.seeded(NULL::text, 'anon.fake_first_name()')") is None


@pytest.mark.parametrize("value", SAMPLE_EMAILS)
def test_fake_email_deterministic_and_not_identity(cur, value):
    out = _det(cur, "odoo_synth.fake_email", value)
    assert out is not None
    assert out != value
    assert "@" in out
    assert out.lower().endswith("@example.invalid"), out
    # Real domain must not leak.
    real_domain = value.split("@", 1)[1].lower() if "@" in value else ""
    assert real_domain not in out.lower()


def test_fake_email_null_passthrough(cur):
    assert _scalar(cur, "SELECT odoo_synth.fake_email(NULL::text)") is None


@pytest.mark.parametrize("value", SAMPLE_VATS)
def test_fake_vat_preserves_format(cur, value):
    out = _det(cur, "odoo_synth.fake_vat", value)
    assert out is not None and out != value
    prefix = value[:2]
    if prefix.isalpha():
        assert out[:2] == prefix.upper()
        assert out[2:].isdigit()
        assert len(out[2:]) == len(value) - 2, f"digit count changed: {value} -> {out}"
    else:
        # no country prefix: digit positions preserved, non-digits kept
        assert len(out) == len(value)


def test_fake_vat_null_passthrough(cur):
    assert _scalar(cur, "SELECT odoo_synth.fake_vat(NULL::text)") is None


@pytest.mark.parametrize("value", SAMPLE_IBANS)
def test_fake_iban_mod97_valid_and_format_preserving(cur, value):
    out = _det(cur, "odoo_synth.fake_iban", value)
    assert out is not None and out != value
    # Country code + total length preserved.
    assert out[:2] == value[:2].upper(), f"country code changed: {value} -> {out}"
    assert len(out) == len(value), f"length changed: {value}({len(value)}) -> {out}({len(out)})"
    # The real acceptance criterion: output passes the generic mod-97 check,
    # verified by an INDEPENDENT oracle (python-stdnum), not the same formula
    # fake_iban used to generate the check digits.
    assert _iban_generic_mod97_ok(out), f"fake_iban output fails mod-97: {out}"


def test_fake_iban_known_limitations(cur):
    """Documents what fake_iban does NOT guarantee, asserted not assumed.

    The generic mod-97 is satisfied (tested above with an independent
    oracle); stricter per-country BBAN validators are not. GB fakes lack a
    valid UK sort-code structure; NL fakes fail the Dutch elfproef. This is
    the known limitation spelled out in sql/bootstrap.sql's header and
    TESTING.md -- asserted here so a future change that accidentally makes
    fakes country-valid (or one that breaks the generic check) gets caught.
    """
    # GB and NL both have country-specific BBAN checks in python-stdnum.
    gb = _det(cur, "odoo_synth.fake_iban", "GB29NWBK60161331926819")
    nl = _det(cur, "odoo_synth.fake_iban", "NL91ABNA0417164300")
    # Generic mod-97 must hold for both (the actual contract).
    assert _iban_generic_mod97_ok(gb), f"GB fake fails generic mod-97: {gb}"
    assert _iban_generic_mod97_ok(nl), f"NL fake fails generic mod-97: {nl}"
    # And the strict per-country checks are NOT guaranteed to pass -- if
    # they ever do start passing that's fine, but we must not regress the
    # generic check. We assert the limitation is real (at least one of GB/NL
    # fails strict) so the documented claim is verified, not folklore.
    strict_results = {v: _iban_strict_country_ok(v) for v in (gb, nl)}
    assert not all(strict_results.values()), (
        "fake_iban's documented per-country limitation appears to no longer "
        f"hold: strict validation results {strict_results}. If fake_iban now "
        "satisfies per-country BBAN checks too, update sql/bootstrap.sql and "
        "TESTING.md to drop the limitation note -- this is a strict improvement."
    )


def test_fake_iban_null_passthrough(cur):
    assert _scalar(cur, "SELECT odoo_synth.fake_iban(NULL::text)") is None


def test_fake_iban_non_iban_falls_back(cur):
    # Non-IBAN input: should fall back to fake_bank_account (digits, same length).
    inp = "12345678"
    out = _det(cur, "odoo_synth.fake_iban", inp)
    assert out is not None and out != inp
    assert len(out) == len(inp)
    assert out.isdigit()


@pytest.mark.parametrize("value", ["12345678", "ACC-9999", "12-char-acct"])
def test_fake_bank_account_deterministic_same_length(cur, value):
    out = _det(cur, "odoo_synth.fake_bank_account", value)
    assert out is not None and out != value
    assert len(out) == len(value)


def test_fake_bank_account_null_passthrough(cur):
    assert _scalar(cur, "SELECT odoo_synth.fake_bank_account(NULL::text)") is None


@pytest.mark.parametrize("value", SAMPLE_PHONES)
def test_fake_phone_deterministic_and_not_identity(cur, value):
    out = _det(cur, "odoo_synth.fake_phone", value)
    assert out is not None and out != value


def test_fake_phone_null_passthrough(cur):
    assert _scalar(cur, "SELECT odoo_synth.fake_phone(NULL::text)") is None


@pytest.mark.parametrize("value", SAMPLE_TEXTS)
def test_redact_text_not_identity_and_length_bounded(cur, value):
    a = _scalar(cur, "SELECT odoo_synth.redact_text(%s)", (value,))
    b = _scalar(cur, "SELECT odoo_synth.redact_text(%s)", (value,))
    assert a is not None
    assert a == b, "redact_text should be deterministic for a given input"
    assert a != value
    # Placeholder is trimmed to input length (or 1 for empty).
    assert len(a) == max(len(value), 1)


def test_redact_text_null_passthrough(cur):
    assert _scalar(cur, "SELECT odoo_synth.redact_text(NULL::text)") is None
