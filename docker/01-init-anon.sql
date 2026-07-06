-- Auto-run on first init of the postgres-anon scratch container, via
-- docker-entrypoint-initdb.d. Installs the anon extension, initializes
-- its faker dictionary, and loads the odoo_synth helper functions from
-- the repo's sql/bootstrap.sql (mounted read-only into the init dir).
CREATE EXTENSION IF NOT EXISTS anon;
SELECT anon.init();
