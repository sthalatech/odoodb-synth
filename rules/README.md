# odoo-synth rulebook

This directory is the actual community deliverable. The masking *engine*
(`anon` extension) already exists and is maintained by people who know
Postgres internals far better than an Odoo-focused project needs to.
What doesn't exist yet is an accurate, maintained map of **which Odoo
fields are sensitive** — that's what these files are.

## Layout

Files are numbered so `odoo-synth` applies them in a predictable order
and so reviewers can find things by app area:

| File | Covers |
|---|---|
| `00_strategies.yml` | The masking-strategy vocabulary. Read this first — everything else just assigns strategies from here to fields. |
| `05_patterns.yml` | Column-name pattern backstop (denormalized cache fields) — see "Pattern rules" below. Loaded before per-model files. |
| `10_core.yml` | `res.partner`, `res.users`, `res.company`, bank accounts |
| `20_accounting.yml` | Invoices, payments, payment tokens/providers |
| `21_operations.yml` | High-value operational models (`account.journal`, `stock.move`/`stock.picking`, `ir.sequence`, `res.bank`, `sale.order.line`) — explicit reviewed entries, not pattern-matched |
| `30_hr.yml` | Employees, contracts, recruitment |
| `40_messaging.yml` | Chatter, mail, activities, calendar — the free-text problem lives here |
| `50_attachments.yml` | Binary file policy (not per-field, per-model) |
| `55_module_metadata.yml` | Org-name leaks in module metadata: `ir.module.module.author` (global null) + targeted `ir.ui.view.arch_db` string-replace for custom report templates that hardcode the org name |
| `60_system_secrets.yml` | API keys, SMTP credentials, signing secrets — security hygiene, not PII |
| `70_sales_crm_website.yml` | Leads, orders, newsletter lists, surveys |

## Pattern rules (the denormalized-cache backstop)

`05_patterns.yml` adds a `column_patterns:` list: rules that match by
column **name** regex regardless of model, scoped by PII shape, and apply
a strategy. This exists because Odoo denormalizes `res.partner.name` /
`.email` into dozens of cache columns across hundreds of models
(`account_move.invoice_partner_display_name`,
`res_partner.complete_name`/`commercial_company_name`, `ir_sequence.name`,
...) — listing every one per-model doesn't scale, and the v0.1.0 darkstore
run confirmed these caches are the #1 leak surface.

Pattern rules are a **backstop, not a substitute for reviewing what's
declared**:

* `rules scan` lists every pattern-matched column under **"Covered by
  pattern"** — they are visible in the report, not silently dropped. You
  can see exactly what a pattern caught and audit it.
* A per-model field rule **always wins** over a pattern. Add explicit
  entries (like `21_operations.yml`) for high-value models whose field
  semantics you've actually reviewed; the pattern only catches what no
  explicit rule covers.
* Patterns are scoped by `shapes:` (`free_text`, `partner_ref`, `binary`)
  so a suffix like `_display_name` only fires on text columns, not an
  unrelated integer column that happens to share the name.

The shipped patterns use `redact_freetext`, not a `fake_*` strategy:
a cache field can hold a person OR a company name, and redaction is
length-preserving and never leaks a substring — the honest default per
the "free-text problem" section above. Where a cache is reviewed and
structurally known (e.g. an explicit `account.move` entry), a `fake_*`
strategy is fine; patterns are for the unreviewed 876+ models.


## The free-text problem (read this before trusting any redact_freetext rule)

Structured fields (`email`, `phone`, `vat`) are the easy 80%. Free-text
fields — chatter bodies, notes, descriptions, survey answers — are where
this whole approach gets honest about its limits. A support agent can
type a customer's home address into a plain `note` field on any model,
and no schema-level rule will ever catch that.

Two honest options, both included as strategies:

1. **`redact_freetext`** (default for anything explicitly flagged below):
   nuke the content, replace with placeholder text of similar length.
   Loses realistic UI content, but guarantees no leakage.
2. **Leave it and accept the residual risk** — only ever appropriate for
   fields you've reviewed and are confident don't accumulate free-typed
   PII in your specific instance (e.g. internal-only fields with a
   constrained editor population).

There is no `strategy: nlp_scrub_and_hope` in this rulebook on purpose.
An NER-based scrubber is a legitimate v2 feature, but shipping it as a
*default* would create false confidence — it will miss things, and
"probably mostly redacted" is a worse security posture than "definitely
redacted" for a tool whose entire purpose is safety.

## Extending this rulebook

Every OCA/enterprise module you install adds models with fields nobody's
reviewed yet. That's what `odoo-synth rules scan` is for — it flags new
`Char`/`Text`/`Many2one(res.partner)` fields on any installed model that
aren't yet declared `keep` or given a strategy here, and `rules diff`
runs the same check in CI against a schema snapshot so this rulebook
doesn't silently rot as your instance evolves.

If you add a rule file for an OCA or paid module, name it
`8x_<module_name>.yml` and PR it — that's the whole point of this being
open source instead of everyone's private, incomplete script.
