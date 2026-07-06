# odoo-synth

Masks PII in an Odoo v19 database backup and provisions a fresh,
prod-shaped Odoo instance from the masked result — so developers (and
debugging agents) get realistic data to work against without ever
touching real customer information.

**Status:** early scaffolding stage. `AGENT_PROMPT.md` is the spec
this repo was built from; `TESTING.md` is how to verify any of it
before trusting it with real data.

## How it works, in one paragraph

Pull a backup (self-hosted `pg_dump`/`odoo-bin db dump`, or a manual
export from odoo.sh) → never mask in place, always on a scratch
database → mask using the `postgresql_anonymizer` (`anon`) extension,
driven by the declarative rulebook in `rules/` → package the masked
result as a restorable backup → provision a fresh instance with
`odoo-bin db load --neutralize`.

## Prerequisites

- **Docker + Docker Compose** — runs the scratch Postgres (with `anon`
  installed) and an Odoo 19 instance for local testing.
- **Python 3.11+**
- **PostgreSQL client tools** (`pg_dump`, `pg_restore`, `psql`)
  matching your source instance's Postgres major version.
- **Access to an Odoo v19 backup** — either direct DB/shell access for
  a self-hosted instance, or a manually downloaded backup zip from
  odoo.sh's Backups tab. See `AGENT_PROMPT.md` for how the two
  adapters differ.
- **DuckDB** (`pip install duckdb`) — optional, only needed if you
  want the secondary Parquet export alongside the primary `pg_dump`
  artifact.

## Install

```bash
git clone <your-fork-url> odoo-synth
cd odoo-synth

# Python package + CLI
python3 -m venv .venv && source .venv/bin/activate
pip install -e .

# Scratch Postgres (with the anon extension) + an Odoo 19 instance
# with demo data, for local development and testing
docker compose -f docker/docker-compose.scratch.yml up -d

# Load the odoo_synth SQL functions (fake_vat, fake_iban, seeded(),
# redact_text, ...) that rules/00_strategies.yml calls by name
psql "$SCRATCH_DB_URL" -f sql/bootstrap.sql

# Confirm the CLI is wired up
odoo-synth --help

# Sanity-check the rulebook itself before pointing this at any data —
# catches typo'd strategy names and conflicting rules across files
odoo-synth rules validate --rules rules/
```

No `docker compose` available? `docker/docker-compose.scratch.yml`
defines two services (`postgres-anon`, `odoo`) — read it for the
equivalent standalone `docker run` invocations.

## Quickstart

**Self-hosted:**
```bash
odoo-synth snapshot --db my_prod_db --rules rules/ --out snap-$(date +%F)/
odoo-synth up --from snap-$(date +%F)/
```

**odoo.sh (v1 — manual export):**
```bash
# 1. In the odoo.sh Backups tab, download a production backup zip.
odoo-synth ingest --zip backup.zip
odoo-synth up --from <ingested-bundle-path>
```

## Repo layout

| Path | What's there |
|---|---|
| `rules/` | The actual PII rulebook — start with `rules/README.md` |
| `sql/bootstrap.sql` | The masking functions the rulebook calls by name |
| `docker/` | Scratch Postgres + Odoo compose setup |
| `TESTING.md` | How to verify this before trusting it with real data |
| `AGENT_PROMPT.md` | The spec this scaffold was built from |

## Before running this against real data

Read the last section of `TESTING.md`. This tool is only as good as
the rulebook's coverage of *your* installed modules — run
`odoo-synth rules scan` against your actual instance first and review
what it flags before trusting a masked export of anything real.

## Contributing

Rule gaps are the expected failure mode as Odoo installs grow more
modules over time — if `rules scan`/`rules diff` flags something on
your instance that isn't covered, a PR adding it to the relevant
`rules/*.yml` file (or a new `8x_<module_name>.yml`) is the single
most useful contribution this project can take.

## License

Apache-2.0 — see `LICENSE`.
