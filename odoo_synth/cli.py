"""odoo-synth CLI entrypoint.

Wires up every subcommand defined in AGENT_PROMPT.md's CLI surface.
`rules validate` is fully implemented (P0). The other subcommands exist
and appear in --help, but raise NotImplementedError pointing at the
relevant P1 item until they are built -- they never silently no-op or
fake success.
"""

from __future__ import annotations

import sys
from typing import Optional

import typer

from .core.rulebook import RulebookError, load_and_validate, validate_directory

app = typer.Typer(
    name="odoo-synth",
    help=(
        "Masks PII in an Odoo v19 database backup and provisions a fresh, "
        "prod-shaped Odoo instance from the masked result.\n\n"
        "See rules/README.md for the PII rulebook and AGENT_PROMPT.md for the "
        "full design."
    ),
    no_args_is_help=True,
    add_completion=False,
)

rules_app = typer.Typer(
    name="rules",
    help="Inspect and validate the PII rulebook (rules/*.yml).",
    no_args_is_help=True,
)
app.add_typer(rules_app, name="rules")


def _fail(msg: str, code: int = 1) -> None:
    typer.secho(f"error: {msg}", fg=typer.colors.RED, err=True)
    raise typer.Exit(code=code)


# ---------------------------------------------------------------------------
# rules validate  (P0 -- fully implemented)
# ---------------------------------------------------------------------------


@rules_app.command("validate")
def rules_validate(
    rules: str = typer.Option(
        "rules/",
        "--rules",
        help="Path to the rules/ directory (default: rules/).",
    ),
) -> None:
    """Validate the rulebook: parse, check strategy names, check conflicts."""
    try:
        summary = validate_directory(rules)
    except RulebookError as exc:
        _fail(str(exc))
        return  # unreachable, _fail exits
    typer.secho(
        f"OK: rulebook valid -- {summary['strategies']} strategies, "
        f"{summary['field_rules']} field rules across {summary['models']} "
        f"models in {summary['files']} files.",
        fg=typer.colors.GREEN,
    )


# ---------------------------------------------------------------------------
# rules scan / rules diff  (P1 -- TODO, explicit)
# ---------------------------------------------------------------------------


@rules_app.command("scan")
def rules_scan(
    bundle: str = typer.Option(..., "--bundle", help="Path to an ingested bundle to scan."),
    rules: str = typer.Option("rules/", "--rules", help="Path to the rules/ directory."),
) -> None:
    """Flag undeclared PII-shaped fields in a schema snapshot."""
    raise NotImplementedError(
        "rules scan is not implemented yet -- P1 item #12/#8 in the phase-2 "
        "plan. It will flag new Char/Text/Many2one(res.partner) fields on "
        "installed models that aren't declared `keep` or given a strategy. "
        "For now, run `odoo-synth rules validate` to check the rulebook's "
        "internal consistency."
    )


@rules_app.command("diff")
def rules_diff(
    bundle: str = typer.Option(..., "--bundle", help="Path to a schema snapshot bundle."),
    rules: str = typer.Option("rules/", "--rules", help="Path to the rules/ directory."),
) -> None:
    """CI gate: diff a schema snapshot against the rulebook's coverage."""
    raise NotImplementedError(
        "rules diff is not implemented yet -- P1 item #12/#8 in the phase-2 "
        "plan. It runs the same check as `rules scan` against a schema "
        "snapshot in CI so the rulebook doesn't silently rot as your "
        "instance evolves."
    )


# ---------------------------------------------------------------------------
# ingest / snapshot / up  (P1 -- TODO, explicit, with stubs that fail loud)
# ---------------------------------------------------------------------------


@app.command("ingest")
def ingest(
    zip: str = typer.Option(..., "--zip", help="Path to a manually downloaded odoo.sh backup zip."),
) -> None:
    """odoo.sh path: validate manifest.json in a backup zip and unzip into a bundle."""
    raise NotImplementedError(
        "ingest is not implemented yet -- P1 item #10 (adapters/odoo_sh.py). "
        "It will validate the backup zip's manifest.json (odoo-version and "
        "module list) and unzip it into a restorable bundle. The deferred "
        "SSH pull path (pull_via_ssh) is a separate v2 stub."
    )


@app.command("snapshot")
def snapshot(
    db: str = typer.Option(..., "--db", help="Name of the source Odoo database to snapshot."),
    rules: str = typer.Option("rules/", "--rules", help="Path to the rules/ directory."),
    out: str = typer.Option(..., "--out", help="Output directory for the masked bundle."),
) -> None:
    """Self-hosted path: dump, mask on a scratch DB, and package the result."""
    # Validate the rulebook up front -- cheap and catches typos before any
    # heavy lifting. The dump/mask/package steps themselves are P1.
    try:
        load_and_validate(rules)
    except RulebookError as exc:
        _fail(f"rulebook invalid: {exc}")
        return
    raise NotImplementedError(
        "snapshot (dump -> mask -> package) is not implemented yet -- P1 "
        "items #8/#9/#11 (core/mask.py, adapters/self_hosted.py, "
        "core/package.py). The rulebook passed validation; the remaining "
        "pipeline is what's missing."
    )


@app.command("up")
def up(
    from_: str = typer.Option(..., "--from", help="Path to a masked bundle to provision."),
) -> None:
    """Provision a fresh Odoo instance from a masked bundle (with --neutralize)."""
    raise NotImplementedError(
        "up (provision + launch) is not implemented yet -- P1 item #11 "
        "(core/provision.py). It will restore the masked pg_dump, run "
        "`odoo-bin db load --neutralize`, and launch the Odoo container. "
        "Per the guardrails, a provisioned instance must never boot without "
        "--neutralize."
    )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
