# v0.1.0

First tagged release. Rulebook-driven PII masking for Odoo v19
databases, built on `postgresql_anonymizer`.

## What's in this release

- **Rulebook**: 145 field rules across 32 models, 8 files
  (`rules/`). Covers core partner/user data, accounting/payments, HR,
  messaging/chatter (including the `mail.tracking.value` audit-trail
  gap most hand-rolled anonymization scripts miss), attachments, and
  system secrets.
- **Masking function library** (`sql/bootstrap.sql`): deterministic
  pseudonymization (`seeded()`), format-preserving VAT/IBAN/phone
  fakes, free-text redaction. Verified independently: determinism
  holds across separate connections; `fake_iban` checksum validity
  verified against `python-stdnum` as an independent oracle, not the
  same formula used to generate it.
- **CLI**: `ingest` (odoo.sh manual-export path), `snapshot`
  (self-hosted), `up` (provision), `rules validate` / `rules scan` /
  `rules diff` (coverage gap detection against a schema snapshot).
- **Coverage tooling** (`core/coverage.py`, `core/schema.py`): flags
  models/fields with no rulebook entry. This isn't hypothetical — see
  Known Issues below, it caught a real gap in this release's own
  rulebook.

## Known issues

- **`crm.lead.name` and `crm.lead.partner_id` are not covered by any
  masking rule.** Found by this release's own `rules scan` tooling
  against a test schema. `crm.lead.name` (the opportunity title)
  routinely contains a client's name or deal specifics. Tracked for
  v0.2; if you're running this against real CRM data before then,
  add coverage for these two fields to `rules/70_sales_crm_website.yml`
  before trusting a `crm.lead` masked export.
- **No full-stack end-to-end run has completed successfully yet.**
  The integration tests that boot a real Odoo instance from a masked
  snapshot and leak-scan the result (`TESTING.md` layer 3/4) skip on
  disk-constrained CI runners in this release. The masking function
  library and the CLI plumbing are independently unit-tested, but the
  complete pipeline — real backup in, real bootable masked instance
  out — has not been proven end-to-end. Treat this release as
  "the components work," not "the product has been run for real."
- **`fake_iban` is not valid against strict per-country BBAN checks**
  for at least GB and NL (UK sort-code structure, Dutch elfproef).
  Satisfies the generic ISO 7064 mod-97 checksum Odoo's own field
  validator runs. Documented and regression-tested in
  `sql/bootstrap.sql` / `tests/unit/test_sql_functions.py`.
- **`odoo-synth ingest` for odoo.sh is manual-export only.** SSH
  automation (`pull_via_ssh`) is intentionally deferred — the only
  `NotImplementedError` left in the codebase.
- **Only tested against Odoo v19 demo data structurally**, not
  against a real production database. See `TESTING.md`'s "before you
  run this against real data" section before pointing this at
  anything real.

## Recommended before relying on this in practice

Run `odoo-synth rules scan` against your own instance's schema, not
just the demo/test schema this release was developed against —
`crm.lead` proves the coverage gaps aren't theoretical, and modules
you have installed that aren't in core Odoo definitely aren't covered
by the shipped rulebook yet.
