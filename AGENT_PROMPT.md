# Agent prompt: scaffold the odoo-synth repository

Paste everything below into Claude Code (or another coding agent) in an
empty repo directory. The 8 rulebook files referenced below already
exist — copy the ones the user has provided into `rules/` verbatim.
Do not regenerate or edit their content; they were authored and
reviewed separately from this scaffolding pass.

---

## Objective

Build `odoo-synth`, an open-source CLI that takes an Odoo v19 database
backup (from a self-hosted instance or an odoo.sh export), masks all
PII/sensitive data using the PostgreSQL `anon` extension driven by a
declarative rulebook, and provisions a fresh Odoo instance from the
masked result — so developers get a prod-shaped environment to debug
against without ever seeing real customer data.

## Files already provided — place these verbatim

Copy these into `rules/` exactly as given, do not alter content:
`README.md`, `00_strategies.yml`, `10_core.yml`, `20_accounting.yml`,
`30_hr.yml`, `40_messaging.yml`, `50_attachments.yml`,
`60_system_secrets.yml`, `70_sales_crm_website.yml`.

## Repository structure to create

```
odoo-synth/
  README.md                    # project overview, quickstart, links to rules/README.md
  LICENSE                       # Apache-2.0
  pyproject.toml                # Python package, typer or click for the CLI, psycopg
  odoo_synth/
    __init__.py
    cli.py                       # entrypoint, wires up all subcommands below
    adapters/
      __init__.py
      self_hosted.py              # odoo-bin db dump/load if available, else pg_dump -Fd -j N + filestore rsync
      odoo_sh.py                   # v1: ingest a manually-downloaded backup zip. Stub a `pull_via_ssh()` function that raises NotImplementedError with a clear message — this is the deferred v2 path, don't build it now.
    core/
      __init__.py
      rulebook.py                  # loads rules/*.yml, validates cross-file consistency (see Validation below)
      mask.py                       # generates SECURITY LABEL statements from the loaded rulebook, calls anon.anonymize_database()
      package.py                    # produces the output artifact: pg_dump custom-format file (primary) + optional per-table Parquet export via DuckDB (secondary, --with-parquet flag)
      provision.py                  # fresh Postgres + pg_restore + odoo-bin db load --neutralize, launches Odoo container
  rules/                            # <- the 8 files provided go here
  sql/
    bootstrap.sql                   # creates the `odoo_synth` schema and functions referenced by 00_strategies.yml (see spec below)
    apply_shuffles.sql               # handles the shuffle_within_column strategy separately, since it operates across rows not per-value
  docker/
    docker-compose.scratch.yml        # scratch Postgres with anon installed + an Odoo 19 service, used for local dev and integration tests
  tests/
    unit/
      test_sql_functions.py            # see TESTING.md section 2
      test_rulebook_validation.py       # see TESTING.md section 1
    integration/
      test_end_to_end.py                 # see TESTING.md section 3
      test_round_trip_provision.py        # see TESTING.md section 4
  .github/workflows/ci.yml              # runs unit + rulebook tests on every PR; integration tests nightly (they spin real containers)
  TESTING.md                              # write this file — human-readable version of the test plan below, plus exact commands to run each layer locally
```

## `sql/bootstrap.sql` — function specs

Implement these in an `odoo_synth` schema, `CREATE SCHEMA IF NOT
EXISTS odoo_synth;`. Do not assume specific `anon.*` pseudonymization
function names beyond what you can confirm — run `\df anon.*` against
the actually-installed extension version and use what exists. If a
dedicated deterministic/pseudonymization helper exists in the
installed version, prefer it over the wrapper below and note that in
a comment.

**`odoo_synth.seeded(source_value text, expr text) returns text`**
Deterministic wrapper: hashes `source_value` to a seed in the [-1, 1]
range `setseed()` expects, calls `setseed()`, then evaluates `expr`
(a SQL expression string invoking a plain `anon.fake_*()` call) via
`EXECUTE`. Same `source_value` in the same session must always
produce the same output — this is the actual acceptance criterion,
write a test for it, don't just trust the mechanism. Return `NULL` on
`NULL` input without evaluating `expr`.

**`odoo_synth.fake_email(source_value text) returns text`**
Deterministic (same technique as `seeded`), returns a syntactically
valid email at a clearly-fake domain (e.g. `@example.invalid` or a
domain you control), not the real domain from the input.

**`odoo_synth.fake_vat(source_value text) returns text`**
Extract the leading letter(s) matching the country-prefix convention
(e.g. `^[A-Z]{2}`) if present, keep it, deterministically replace the
remaining digits with the same digit count. Full per-country checksum
validity is a stretch goal, not a v1 requirement — flag this
explicitly in a code comment and in `TESTING.md` as a known
limitation so nobody assumes VAT checksums are guaranteed valid.

**`odoo_synth.fake_iban(source_value text) returns text`**
Extract the ISO country code (first 2 chars) and total length, keep
both, deterministically generate the BBAN portion, and recompute the
mod-97 check digits per ISO 7064 so the result passes standard IBAN
validation (rearrange country code + check digits to the end,
convert letters to numbers A=10..Z=35, compute mod 97, check digits
= 98 − remainder). This one should actually be correct, not
best-effort — Odoo's IBAN validator will reject malformed output and
that's a real functional bug, not just a masking nicety.

**`odoo_synth.fake_bank_account(source_value text) returns text`**
Non-IBAN fallback: same length, deterministic, digits only.

**`odoo_synth.fake_phone(source_value text) returns text`** —
wrapper around `seeded()` calling a plain-number generator.

**`odoo_synth.redact_text(source_value text) returns text`**
Return placeholder text of roughly the same length as the input
(e.g. repeated lorem-ipsum words trimmed to length), or a fixed
short placeholder if input is short. Must not leak any substring of
the original beyond incidental short common words.

## CLI surface (`odoo_synth/cli.py`)

```
odoo-synth ingest    --zip <path>                         # odoo.sh path: validate manifest.json, unzip
odoo-synth snapshot  --db <name> --rules rules/            # self-hosted path: dump, mask, package
odoo-synth rules validate --rules rules/                    # see Validation below
odoo-synth rules scan     --bundle <dir>                     # flag undeclared PII-shaped fields
odoo-synth rules diff     --bundle <dir> --rules rules/       # CI gate against a schema snapshot
odoo-synth up         --from <masked-bundle>                  # provision + launch
```

## Validation `rules validate` must perform

- Every YAML file under `rules/` parses without error.
- Every `strategy:` value used anywhere in `10_*.yml` through
  `70_*.yml` exists as a key under `strategies:` in
  `00_strategies.yml`. Fail loudly with file:line if not.
- No `model.field` is declared with two different strategies across
  files (catch copy-paste drift as the rulebook grows).
- Every `sql_template` in `00_strategies.yml` that references
  `{column}` is only used in contexts where that substitution makes
  sense (basic sanity check, not full SQL parsing).

## Guardrails — do not violate these

- Never mask in place. Every operation in `mask.py` must run against
  a scratch database, never the source connection passed to `ingest`
  or `snapshot`.
- `provision.py` must call `odoo-bin db load --neutralize` (or
  equivalent) — never boot a provisioned instance without it.
- Don't invent `anon` API surface you haven't confirmed against the
  installed version's `\df anon.*` output.
- Default attachment policy is drop-content-keep-metadata per
  `rules/50_attachments.yml` — don't change this default while
  scaffolding.
- License is Apache-2.0, not AGPL — this is a standalone tool, not an
  Odoo module loaded into a running server.

## Definition of done

- `pip install -e .` works and `odoo-synth --help` lists all
  subcommands.
- `docker compose -f docker/docker-compose.scratch.yml up` brings up
  a Postgres with `anon` installed and an Odoo 19 instance with demo
  data, reachable at `localhost:8069`.
- `odoo-synth rules validate` passes against the provided rulebook.
- `TESTING.md` exists and documents exact commands for every layer in
  the test plan — write this as you build each piece, not at the end.
