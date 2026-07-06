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
| `10_core.yml` | `res.partner`, `res.users`, `res.company`, bank accounts |
| `20_accounting.yml` | Invoices, payments, payment tokens/providers |
| `30_hr.yml` | Employees, contracts, recruitment |
| `40_messaging.yml` | Chatter, mail, activities, calendar — the free-text problem lives here |
| `50_attachments.yml` | Binary file policy (not per-field, per-model) |
| `60_system_secrets.yml` | API keys, SMTP credentials, signing secrets — security hygiene, not PII |
| `70_sales_crm_website.yml` | Leads, orders, newsletter lists, surveys |

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
