"""Generate SECURITY LABEL statements from the loaded rulebook and apply them.

GUARDRAIL (from AGENT_PROMPT.md): never mask in place. Every function in
this module must run against a scratch database connection, never the
source connection passed to ingest/snapshot. The caller is responsible
for handing us a scratch DB connection restored from the source dump.

P1 item #8 -- not implemented yet. The plan, for when this is built:

  1. Load the rulebook via core.rulebook.load_and_validate().
  2. For each FieldRule whose strategy has a non-null sql_template,
     substitute {column} with the physical column name and emit:
        SECURITY LABEL ON COLUMN <table>.<column> IS '<template>';
     (postgresql_anonymizer maps SECURITY LABEL strings to masking rules.)
  3. Call SELECT anon.anonymize_database(); to apply all labels.
  4. Run the shuffle statements (sql/apply_shuffles.sql mechanism) for any
     strategy: shuffle_within_column rules -- these are cross-row, not
     per-value, so they happen after anonymize_database().
  5. Handle 60_system_secrets.yml rotate_secret entries by emitting
     explicit UPDATE ... SET col = <fresh random> statements (rotate, not
     derive).
"""

from __future__ import annotations

from .rulebook import Rulebook


class MaskError(Exception):
    """Raised when masking cannot proceed (e.g. connected to a source DB)."""


def generate_security_labels(rulebook: Rulebook) -> list[str]:
    """TODO P1 #8: emit SECURITY LABEL statements from the rulebook."""
    raise NotImplementedError(
        "mask.generate_security_labels is not implemented yet -- P1 item #8. "
        "It will translate each FieldRule's strategy sql_template into a "
        "SECURITY LABEL ON COLUMN statement for postgresql_anonymizer."
    )


def apply_masking(scratch_db_url: str, rulebook: Rulebook) -> None:
    """TODO P1 #8: apply masking to a SCRATCH database (never the source).

    The scratch_db_url MUST point at a database restored from the source
    dump, not the source itself. This function will refuse to run against
    any connection it cannot confirm is a scratch DB.
    """
    raise NotImplementedError(
        "mask.apply_masking is not implemented yet -- P1 item #8. "
        "Per the guardrails, masking only ever runs against a scratch "
        "database restored from the source dump, never the source."
    )
