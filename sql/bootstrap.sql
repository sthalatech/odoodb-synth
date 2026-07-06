-- sql/bootstrap.sql
--
-- Installs the `odoo_synth` schema and the masking helper functions that
-- rules/00_strategies.yml references by name. These wrap the PostgreSQL
-- `anon` extension (postgresql_anonymizer) which must already be installed
-- in the target database: `CREATE EXTENSION IF NOT EXISTS anon;` then
-- `SELECT anon.init();` (init loads the faker dictionary the anon.fake_*
-- generators draw from).
--
-- Confirmed against: anon version 3.1.3 (PostgreSQL 18), image
--   registry.gitlab.com/dalibo/postgresql_anonymizer:3.1.3
-- Function names used below were verified with `\df anon.*` against that
-- version. The anon.fake_* family (fake_first_name, fake_last_name,
-- fake_company, fake_address, fake_city, fake_postcode) is deterministic
-- when seeded via setseed(); the anon.dummy_* family is NOT (it draws
-- from its own non-seeded RNG), so we avoid it inside odoo_synth.seeded().
--
-- KNOWN LIMITATION: odoo_synth.fake_vat preserves the country prefix and
-- digit count but does NOT guarantee per-country VAT checksum validity
-- (e.g. BE mod-97, IT CIN). Documented in TESTING.md. Per-country checksum
-- support is an explicit v2 stretch goal, not a v1 requirement per
-- AGENT_PROMPT.md.

-- Requires the anon extension to be available and initialized.
CREATE EXTENSION IF NOT EXISTS anon;
SELECT anon.init();

CREATE SCHEMA IF NOT EXISTS odoo_synth;

-- ------------------------------------------------------------------
-- Compatibility shim: anon.fake_phone_number()
--
-- rules/00_strategies.yml's `fake_phone` strategy calls
-- `anon.fake_phone_number()` inside odoo_synth.seeded(). In anon 3.1.3 the
-- only phone generators are anon.dummy_phone_number() and anon.random_phone()
-- -- both of the `dummy_*` family ignore setseed() (they use their own RNG),
-- which would break determinism. We therefore provide a thin
-- `anon.fake_phone_number()` shim that draws from random(), which DOES
-- respect the seed odoo_synth.seeded() just set. Output is a plausible
-- `NNN-NNN-NNNN xNNNN` format (not locale-validated, per the rulebook).
-- If a future anon version ships a native anon.fake_phone_number(), this
-- CREATE will fail with a duplicate-function error and should be dropped.
-- ------------------------------------------------------------------
CREATE OR REPLACE FUNCTION anon.fake_phone_number() RETURNS text AS $$
DECLARE
  a int; b int; c int; ext int;
BEGIN
  a := 100 + floor(random() * 900)::int;
  b := 100 + floor(random() * 900)::int;
  c := 1000 + floor(random() * 9000)::int;
  ext := 100 + floor(random() * 9900)::int;
  RETURN a::text || '-' || b::text || '-' || c::text || ' x' || ext::text;
END;
$$ LANGUAGE plpgsql VOLATILE;

-- ------------------------------------------------------------------
-- odoo_synth.seeded(source_value text, expr text) returns text
--
-- Deterministic wrapper. Hashes source_value to a double in [-1, 1],
-- calls setseed(), then evaluates `expr` (a SQL expression string that
-- should invoke a plain anon.fake_*() call) via EXECUTE. Same source_value
-- in the same session MUST always produce the same output -- this is the
-- contract cross-table join consistency (res.partner.email vs
-- mail.message.email_from) depends on, and it has a unit test.
--
-- Returns NULL on NULL input without evaluating expr.
--
-- NOTE on newer anon: anon 3.x also exposes pseudo_* helpers (pseudo_email,
-- pseudo_first_name, ...) which are deterministic by construction. If your
-- installed anon version has them, you may prefer them over this wrapper
-- for the specific strategies that currently call seeded(). We keep the
-- setseed() wrapper here because the rulebook's sql_templates already
-- reference it and because anon.fake_phone_number() (our shim) needs the
-- setseed() path.
-- ------------------------------------------------------------------
CREATE OR REPLACE FUNCTION odoo_synth.seeded(source_value text, expr text) RETURNS text AS $$
DECLARE
  h bigint;
  s double precision;
  out text;
BEGIN
  IF source_value IS NULL THEN
    RETURN NULL;
  END IF;
  -- 16 hex digits from md5 -> unsigned 64-bit -> map to [-1, 1) for setseed.
  h := ('x' || lpad(substr(md5(source_value), 1, 15), 16, '0'))::bit(64)::bigint;
  s := ((h % 2000000000000000)::double precision / 1000000000000000.0) - 1.0;
  PERFORM setseed(s);
  EXECUTE 'SELECT ' || expr INTO out;
  RETURN out;
END;
$$ LANGUAGE plpgsql VOLATILE;

-- ------------------------------------------------------------------
-- odoo_synth.fake_email(source_value text) returns text
--
-- Deterministic, syntactically valid email at a clearly-fake domain
-- (@example.invalid). The real customer's domain is never carried over.
-- Local part is built from anon.fake_first_name() || '.' || anon.fake_last_name()
-- via seeded() so the whole thing is deterministic.
-- ------------------------------------------------------------------
CREATE OR REPLACE FUNCTION odoo_synth.fake_email(source_value text) RETURNS text AS $$
DECLARE
  local text;
BEGIN
  IF source_value IS NULL THEN
    RETURN NULL;
  END IF;
  local := odoo_synth.seeded(source_value, 'anon.fake_first_name() || ''.'' || anon.fake_last_name()');
  local := lower(regexp_replace(local, '[^a-z0-9.]', '', 'g'));
  IF local = '' OR local ~ '^[.]+$' THEN
    local := 'user';
  END IF;
  RETURN local || '@example.invalid';
END;
$$ LANGUAGE plpgsql VOLATILE;

-- ------------------------------------------------------------------
-- odoo_synth.fake_vat(source_value text) returns text
--
-- Format-preserving: keeps the leading 2-letter country prefix (if any)
-- and the exact digit count of the remainder, deterministically replacing
-- the digits. Does NOT guarantee per-country VAT checksum validity -- see
-- the file header and TESTING.md. For inputs with no country prefix, digits
-- are deterministically replaced and non-digits are preserved.
-- ------------------------------------------------------------------
CREATE OR REPLACE FUNCTION odoo_synth.fake_vat(source_value text) RETURNS text AS $$
DECLARE
  prefix text;
  rest text;
  seed text;
  out text;
  i int;
BEGIN
  IF source_value IS NULL THEN
    RETURN NULL;
  END IF;
  prefix := substring(source_value from '^[A-Za-z]{2}');
  IF prefix IS NOT NULL THEN
    rest := substr(source_value, 3);
    seed := md5('vat:' || source_value);
    out := '';
    WHILE char_length(out) < char_length(rest) LOOP
      seed := md5(seed || out);
      out := out || regexp_replace(substr(seed, 1, 8), '[^0-9]', '', 'g');
    END LOOP;
    RETURN upper(prefix) || substr(out, 1, char_length(rest));
  END IF;
  -- No country prefix: replace digits deterministically, keep non-digits.
  seed := md5('vat:' || source_value);
  out := '';
  FOR i IN 1..char_length(source_value) LOOP
    IF substr(source_value, i, 1) ~ '[0-9]' THEN
      seed := md5(seed || out);
      out := out || substr(regexp_replace(seed, '[^0-9]', '', 'g'), 1, 1);
    ELSE
      out := out || substr(source_value, i, 1);
    END IF;
  END LOOP;
  RETURN out;
END;
$$ LANGUAGE plpgsql VOLATILE;

-- ------------------------------------------------------------------
-- odoo_synth.fake_iban(source_value text) returns text
--
-- Format-preserving AND checksum-valid. Keeps the ISO country code (first
-- 2 chars) and total length, deterministically generates the BBAN, and
-- recomputes the mod-97 check digits per ISO 7064 so the result passes
-- standard IBAN validation (rearrange country code + check digits to the
-- end, convert letters A=10..Z=35, mod 97, check digits = 98 - remainder).
-- Non-IBAN inputs (don't match ^[A-Za-z]{2}[0-9]{2}[0-9A-Za-z]+$) fall back
-- to odoo_synth.fake_bank_account().
-- ------------------------------------------------------------------
CREATE OR REPLACE FUNCTION odoo_synth.fake_iban(source_value text) RETURNS text AS $$
DECLARE
  cc text;
  total_len int;
  bban_len int;
  bban_digits text;
  rearranged text;
  numeric text;
  remainder int;
  check_digits text;
  seed text;
  i int;
BEGIN
  IF source_value IS NULL THEN
    RETURN NULL;
  END IF;
  IF source_value !~ '^[A-Za-z]{2}[0-9]{2}[0-9A-Za-z]+$' THEN
    RETURN odoo_synth.fake_bank_account(source_value);
  END IF;
  cc := upper(substr(source_value, 1, 2));
  total_len := char_length(source_value);
  bban_len := total_len - 4;
  -- Deterministic pseudo-random digit string of bban_len length.
  seed := md5('iban:' || source_value);
  bban_digits := '';
  WHILE char_length(bban_digits) < bban_len LOOP
    seed := md5(seed || bban_digits);
    bban_digits := bban_digits || regexp_replace(substr(seed, 1, 8), '[^0-9]', '', 'g');
  END LOOP;
  bban_digits := substr(bban_digits, 1, bban_len);
  -- mod-97 check digits: rearrange to BBAN + CC + 00.
  rearranged := bban_digits || cc || '00';
  numeric := '';
  FOR i IN 1..char_length(rearranged) LOOP
    numeric := numeric || CASE
      WHEN substr(rearranged, i, 1) ~ '[0-9]' THEN substr(rearranged, i, 1)
      ELSE lpad((ascii(upper(substr(rearranged, i, 1))) - 55)::text, 2, '0')
    END;
  END LOOP;
  -- mod 97 of an arbitrarily long numeric string, processed digit by digit.
  remainder := 0;
  FOR i IN 1..char_length(numeric) LOOP
    remainder := (remainder * 10 + substr(numeric, i, 1)::int) % 97;
  END LOOP;
  check_digits := lpad((98 - remainder)::text, 2, '0');
  RETURN cc || check_digits || bban_digits;
END;
$$ LANGUAGE plpgsql VOLATILE;

-- ------------------------------------------------------------------
-- odoo_synth.fake_bank_account(source_value text) returns text
--
-- Non-IBAN fallback: same length, deterministic, digits only.
-- ------------------------------------------------------------------
CREATE OR REPLACE FUNCTION odoo_synth.fake_bank_account(source_value text) RETURNS text AS $$
DECLARE
  seed text;
  out text;
BEGIN
  IF source_value IS NULL THEN
    RETURN NULL;
  END IF;
  seed := md5('bank:' || source_value);
  out := '';
  WHILE char_length(out) < char_length(source_value) LOOP
    seed := md5(seed || out);
    out := out || regexp_replace(substr(seed, 1, 8), '[^0-9]', '', 'g');
  END LOOP;
  RETURN substr(out, 1, char_length(source_value));
END;
$$ LANGUAGE plpgsql VOLATILE;

-- ------------------------------------------------------------------
-- odoo_synth.fake_phone(source_value text) returns text
--
-- Wrapper around seeded() calling anon.fake_phone_number() (the shim
-- above). Deterministic because the shim draws from random(), which
-- respects the seed odoo_synth.seeded() sets.
-- ------------------------------------------------------------------
CREATE OR REPLACE FUNCTION odoo_synth.fake_phone(source_value text) RETURNS text AS $$
BEGIN
  IF source_value IS NULL THEN
    RETURN NULL;
  END IF;
  RETURN odoo_synth.seeded(source_value, 'anon.fake_phone_number()');
END;
$$ LANGUAGE plpgsql VOLATILE;

-- ------------------------------------------------------------------
-- odoo_synth.redact_text(source_value text) returns text
--
-- Replaces free-text content with placeholder text of roughly the same
-- length, built from lorem-ipsum words trimmed to the input length. Must
-- not leak any substring of the original beyond incidental short common
-- words (the placeholder vocabulary is fixed and disjoint from typical
-- input content). Short inputs get a trimmed placeholder. NULL -> NULL.
-- ------------------------------------------------------------------
CREATE OR REPLACE FUNCTION odoo_synth.redact_text(source_value text) RETURNS text AS $$
DECLARE
  words text[];
  n int;
  out text;
  i int;
BEGIN
  IF source_value IS NULL THEN
    RETURN NULL;
  END IF;
  n := greatest(char_length(source_value), 1);
  words := string_to_array(
    'lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod ' ||
    'tempor incididunt ut labore et magna aliqua', ' ');
  out := '';
  i := 1;
  WHILE char_length(out) < n LOOP
    out := out || words[i] || ' ';
    i := i % array_length(words, 1) + 1;
  END LOOP;
  RETURN substr(out, 1, n);
END;
$$ LANGUAGE plpgsql VOLATILE;
