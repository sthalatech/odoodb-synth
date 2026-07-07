# Testing odoo-synth

Five layers, cheapest and fastest first. Layers 1-3 should run on
every PR. Layer 4 is slower (real containers) — nightly is fine.
Layer 5 isn't automatable at all; read it before you point this at
anything real.

## 1. Rulebook validation (seconds, no database needed)

```
odoo-synth rules validate --rules rules/
```

Checks: every YAML file parses, every `strategy:` referenced by a
model/field entry actually exists in `00_strategies.yml`, and no
`model.field` is declared with conflicting strategies across files.
This is what catches copy-paste drift as the rulebook grows — cheap
enough to run on every commit, and it's the first thing that should
fail if someone adds a rule file with a typo'd strategy name.

## 2. SQL function unit tests (needs bare Postgres + `anon`, no Odoo)

For each function in `sql/bootstrap.sql`, assert:

- **Determinism**: calling it twice with the same input in the same
  session produces identical output. This is the actual contract the
  whole cross-table consistency story depends on — if this test is
  flaky, joins between `res.partner.email` and
  `mail.message.email_from` will silently stop matching after
  masking.
- **Non-identity**: output ≠ input (obviously, but worth asserting —
  a bug that silently no-ops a masking function is worse than one
  that crashes).
- **Format validity**: `fake_vat()` output matches the country-prefix
  pattern and digit count of the input; `fake_iban()` output satisfies
  the generic ISO 7064 mod-97 checksum (Odoo's own IBAN field validator
  runs this check). Note: this is the generic mod-97 check only —
  `fake_iban` is NOT verified against stricter country-specific BBAN
  validators (e.g. python-stdnum's per-country checks; GB fakes lack a
  valid UK sort-code structure, NL fakes fail the Dutch elfproef). That's
  a documented known limitation in sql/bootstrap.sql, not a hidden gap.
  When writing the test, use a reference mod-97 implementation
  independent of the generation code — verifying generated output against
  the same formula that generated it is tautological and won't catch a
  generation bug (there are small Python/Postgres libraries for this —
  don't hand-roll the checksum verification separately from the
  generation, using the same reference implementation both places would
  defeat the purpose of the test).
- **NULL passthrough**: `NULL` in → `NULL` out, no exceptions thrown.

Run with `pytest tests/unit/` against a throwaway Postgres container
(`docker compose -f docker/docker-compose.scratch.yml up postgres`).

## 3. Rule-application integration test (needs Docker: Postgres+anon, Odoo 19 with demo data)

Boot Odoo 19 with demo data loaded, run the full snapshot pipeline
against it, then assert on the *output*, not just "did it crash":

- **Odoo still boots against the masked DB**: `odoo-bin -d masked_db
  -u all --stop-after-init` exits 0. This is the cheapest possible
  check that masking didn't break a constraint, sequence, or
  required field.
- **Leak scan — the actual pass/fail gate**: before masking, dump the
  list of every `res.partner.name`, `res.partner.email`,
  `res.users.login`, and `hr.employee.name` value. After masking,
  `pg_dump` the masked database to plain SQL text and `grep -F` for
  every one of those original values. **Zero matches required.** Any
  match is a hard failure, not a warning.
- **`mail.tracking.value` check**: `SELECT count(*) FROM
  mail_tracking_value WHERE old_value_char IS NOT NULL OR
  new_value_char IS NOT NULL` returns 0 after masking. This is the
  specific gotcha called out in `rules/40_messaging.yml` — worth its
  own named test so a regression here doesn't get buried in a
  generic "masking ran successfully" assertion.
- **Payment credential check**: `payment_token.provider_ref IS NULL`
  and `payment_provider.state = 'disabled'` for all rows.
- **Secret rotation check**: `ir_config_parameter` values for
  `database.secret` and `database.uuid` differ from the source DB's
  values (not just non-null — actually different).

## 4. Full round-trip smoke test

Provision the masked snapshot as an actual fresh Odoo instance and
confirm it's usable, not just that the data looks right in isolation:

- Instance boots and serves HTTP 200 on the login page.
- Login works via whatever reset-password path `provision.py`
  establishes (e.g. a fixed dev password set during `--neutralize`).
- One or two smoke-test actions succeed through the UI or JSON-RPC
  (open a customer record, open an invoice) — confirms the masked
  data didn't just pass SQL-level checks but actually renders
  correctly in Odoo's views.

## 5. Before you point this at a real client database

None of the above tests real PII — Odoo's demo dataset is synthetic
to begin with, so a leak-scan against it only proves the *mechanism*
works, not that it'll catch every shape real data takes.

The first run against an actual production copy should:

- Be done by someone already authorized to handle that data under
  your existing data-access policies — running this tool doesn't
  change who's allowed to see the source database.
- Happen on a machine with no unnecessary network access, so a bug
  that fails to mask something doesn't also mean it left the
  building.
- Re-run the leak-scan from section 3, but with original values
  pulled from *that* database, not the demo dataset — that's the
  actual acceptance test for a specific client's data shape, not a
  general regression check.
- Treat any leak-scan failure as a rulebook gap, not a one-off bug:
  the fix is a new entry in the relevant `rules/*.yml` file, PR'd
  back so the next person's instance doesn't hit the same gap.
