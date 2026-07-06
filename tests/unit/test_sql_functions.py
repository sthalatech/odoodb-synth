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
# mod-97 IBAN verification (reference implementation)
# ---------------------------------------------------------------------------


def _iban_mod97_valid(iban: str) -> bool:
    """ISO 7064 mod-97 check: rearrange, convert letters A=10..Z=35, mod 97 == 1."""
    s = iban.upper().replace(" ", "")
    if len(s) < 5:
        return False
    rearr = s[4:] + s[:4]
    num = ""
    for c in rearr:
        if c.isdigit():
            num += c
        elif c.isalpha():
            num += str(ord(c) - 55)  # A=10..Z=35
        else:
            return False
    # Python handles arbitrary-precision ints, so the whole-string mod is fine.
    return int(num) % 97 == 1


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
    # The real acceptance criterion: output passes mod-97.
    assert _iban_mod97_valid(out), f"fake_iban output fails mod-97: {out}"


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
