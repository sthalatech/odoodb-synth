"""odoo-synth CLI entrypoint.

Wires up every subcommand defined in AGENT_PROMPT.md's CLI surface.
`rules validate`, `rules scan`, `rules diff`, `snapshot`, `up`, and `ingest`
are all implemented. The only remaining stub is `odoo_sh.pull_via_ssh`
(the deferred v2 SSH-automation path) -- it raises NotImplementedError by
design, never silently no-ops.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import typer

from .core.rulebook import RulebookError, load_and_validate, validate_directory
from .core.schema import SchemaSnapshot, load_snapshot
from .core.coverage import CoverageReport, analyze

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
        f"models, {summary['column_patterns']} column patterns, in "
        f"{summary['files']} files.",
        fg=typer.colors.GREEN,
    )


# ---------------------------------------------------------------------------
# rules scan / rules diff  (P1 #8/#12 -- implemented)
# Compares the rulebook against a bundle's schema.json snapshot and flags
# PII-shaped columns the rulebook doesn't cover. See core/coverage.py.
# ---------------------------------------------------------------------------


def _resolve_snapshot(bundle: Path) -> SchemaSnapshot:
    """Load schema.json from a bundle, or raise a clear error if absent.

    `rules scan`/`rules diff` compare the rulebook against a schema snapshot.
    Both producers write schema.json: package() from the live catalog (self-
    hosted), odoo_sh.ingest() from the bundle's dump.sql (odoo.sh). If the
    sidecar is missing the bundle predates this feature -- tell the operator
    to re-run snapshot/ingest rather than guessing at an empty result.
    """
    sidecar = bundle / "schema.json"
    if sidecar.exists():
        return load_snapshot(sidecar)
    raise RulebookError(
        f"{bundle}/schema.json not found -- this bundle has no schema "
        "snapshot. Re-run `odoo-synth snapshot` (self-hosted) or "
        "`odoo-synth ingest` (odoo.sh) to regenerate it. `rules scan`/"
        "`rules diff` need the snapshot to know which columns exist."
    )


@rules_app.command("scan")
def rules_scan(
    bundle: str = typer.Option(..., "--bundle", help="Path to an ingested bundle to scan."),
    rules: str = typer.Option("rules/", "--rules", help="Path to the rules/ directory."),
) -> None:
    """Flag undeclared PII-shaped fields in a schema snapshot.

    Per rules/README.md: flags Char/Text/Many2one(res.partner)/bytea columns on
    installed models that aren't declared `keep` or given a strategy. Use
    after installing a new module to find what the rulebook doesn't cover yet.
    Exits non-zero if any findings, so it's also a usable CI gate.
    """
    bundle_path = Path(bundle)
    if not bundle_path.is_dir():
        _fail(f"bundle not found: {bundle_path}")
        return
    try:
        rb = load_and_validate(rules)
        snap = _resolve_snapshot(bundle_path)
        report = analyze(rb, snap)
    except RulebookError as exc:
        _fail(str(exc))
        return
    _print_scan_report(report)
    if report.has_findings:
        _fail(f"{len(report.findings)} undeclared PII-shaped field(s) found -- "
              "add rules or declare `keep` for the reviewed ones.")
    else:
        typer.secho("OK: rulebook covers every PII-shaped column in the snapshot.",
                    fg=typer.colors.GREEN)


@rules_app.command("diff")
def rules_diff(
    bundle: str = typer.Option(..., "--bundle", help="Path to a schema snapshot bundle."),
    rules: str = typer.Option("rules/", "--rules", help="Path to the rules/ directory."),
) -> None:
    """CI gate: diff a schema snapshot against the rulebook's coverage.

    Same check as `rules scan` but intended for CI: exits non-zero on any
    finding so the rulebook can't silently rot as the instance evolves.
    """
    rules_scan(bundle=bundle, rules=rules,)


def _print_scan_report(report: CoverageReport) -> None:
    """Human-readable findings list + summary."""
    typer.echo(report.summary())
    if report.pattern_matches:
        typer.echo("")
        typer.echo(f"Covered by pattern ({len(report.pattern_matches)}):")
        for f in report.pattern_matches:
            fk = f" -> {f.fk_target}" if f.fk_target else ""
            nn = " NOT NULL" if f.not_null else ""
            typer.echo(
                f"  {f.table}.{f.column}  [{f.shape}] {f.data_type}{nn}{fk}"
                f"\n      {f.reason}"
            )
    if not report.findings:
        return
    typer.echo("")
    typer.echo("Undeclared PII-shaped fields:")
    for f in report.findings:
        fk = f" -> {f.fk_target}" if f.fk_target else ""
        nn = " NOT NULL" if f.not_null else ""
        typer.echo(
            f"  {f.table}.{f.column}  [{f.shape}] {f.data_type}{nn}{fk}"
            f"\n      {f.reason}"
        )


# ---------------------------------------------------------------------------
# ingest / snapshot / up  (P1 -- TODO, explicit, with stubs that fail loud)
# ---------------------------------------------------------------------------


@app.command("ingest")
def ingest(
    zip: str = typer.Option(..., "--zip", help="Path to a manually downloaded odoo.sh backup zip."),
    out: str = typer.Option("bundle/", "--out", help="Output directory for the ingested bundle."),
    rules: str = typer.Option("rules/", "--rules", help="Path to the rules/ directory (for the undeclared-module flag)."),
) -> None:
    """odoo.sh path: validate manifest.json in a backup zip and unzip into a bundle."""
    from .adapters import odoo_sh
    from .adapters.odoo_sh import IngestError
    try:
        out_dir = odoo_sh.ingest(zip, out, rules_dir=rules)
    except IngestError as exc:
        _fail(f"ingest failed: {exc}")
        return
    typer.secho(f"OK: ingested into {out_dir}", fg=typer.colors.GREEN)


# ---------------------------------------------------------------------------
# inspect  (detect source provenance -- the non-data layers a replica needs)
# ---------------------------------------------------------------------------


@app.command("inspect")
def inspect(
    source_db_url: str = typer.Option(
        None, "--source-db-url",
        help="psycopg URL of the SOURCE Odoo DB to inspect (read-only).",
    ),
    odoo_conf: str = typer.Option(
        None, "--odoo-conf",
        help="Path to the source odoo.conf (to resolve addons_path).",
    ),
    addons_path: str = typer.Option(
        None, "--addons-path",
        help="Comma-separated addons_path dirs (overrides/augments odoo.conf).",
    ),
    odoo_bin: str = typer.Option(
        None, "--odoo-bin",
        help="Path to the source odoo-bin (to detect Odoo + Python versions).",
    ),
    out: str = typer.Option(
        None, "--out",
        help="Write provenance.json here (default: print to stdout only).",
    ),
) -> None:
    """Detect the source deployment's shape (Odoo/PG/Python versions, installed
    modules, addon code) and report what a replica needs to reproduce it."""
    import os
    from .core import provenance
    src = source_db_url or os.environ.get("SOURCE_DB_URL")
    if not src:
        _fail("no source DB URL: pass --source-db-url or set SOURCE_DB_URL")
        return
    ap = [s for s in addons_path.split(",")] if addons_path else None
    try:
        report = provenance.detect(
            src, odoo_conf=odoo_conf, addons_path=ap, odoo_bin=odoo_bin,
        )
    except provenance.ProvenanceError as exc:
        _fail(f"inspect failed: {exc}")
        return
    _print_provenance(report)
    if out:
        out_path = Path(out)
        if out_path.is_dir():
            out_path = out_path / "provenance.json"
        out_path.write_text(report.to_json(), "utf-8")
        typer.secho(f"OK: provenance written to {out_path}", fg=typer.colors.GREEN)


def _print_provenance(report) -> None:
    """Human-readable provenance summary."""
    c = typer.colors
    typer.secho("Source provenance", fg=c.CYAN, bold=True)
    typer.echo(f"  DB name         : {report.source_db_name}")
    typer.echo(f"  PostgreSQL      : {report.postgres_version} (major {report.postgres_major})")
    typer.echo(f"  DB encoding     : {report.db_encoding}")
    typer.echo(f"  Odoo series     : {report.odoo_series}  (base {report.odoo_base_version})")
    typer.echo(f"  Odoo-bin        : {report.odoo_bin_version or '(unknown -- pass --odoo-bin)'}")
    typer.echo(f"  Python          : {report.python_version or '(unknown -- pass --odoo-bin)'}")
    typer.echo(f"  Installed mods  : {report.installed_module_count}")
    typer.echo(f"  addons_path     : {len(report.addons_path)} dir(s)")
    for repo in report.addon_repos:
        commit = (repo.git_commit or "")[:12] or "(no git)"
        dirty = " +dirty" if repo.is_dirty else ""
        typer.echo(f"    - {repo.path}  [{repo.addon_count} addons, {commit}{dirty}]")
    if report.missing_addons:
        typer.secho(
            f"  MISSING code    : {len(report.missing_addons)} installed module(s) "
            f"have no code on addons_path (replica blocker):",
            fg=c.RED, bold=True,
        )
        typer.secho(f"    {', '.join(report.missing_addons)}", fg=c.RED)
    elif report.addons_path:
        typer.secho("  Addon code      : all installed modules resolved on addons_path",
                    fg=c.GREEN)
    for w in report.warnings:
        typer.secho(f"  warning: {w}", fg=c.YELLOW)


@app.command("snapshot")
def snapshot(
    db: str = typer.Option(..., "--db", help="Name of the source Odoo database to snapshot."),
    rules: str = typer.Option("rules/", "--rules", help="Path to the rules/ directory."),
    out: str = typer.Option(..., "--out", help="Output directory for the masked bundle."),
    source_db_url: str = typer.Option(
        None, "--source-db-url",
        help="psycopg URL of the SOURCE Odoo DB to dump (never masked in place).",
    ),
    scratch_db_url: str = typer.Option(
        None, "--scratch-db-url",
        help="psycopg URL of the scratch Postgres (with anon) to mask on.",
    ),
    filestore_dir: str = typer.Option(
        None, "--filestore-dir",
        help="Odoo filestore directory (self-hosted pg_dump path only).",
    ),
    odoo_conf: str = typer.Option(
        None, "--odoo-conf",
        help="Path to the source odoo.conf: records provenance (addons_path, "
             "installed-module code presence) into the bundle for the replica.",
    ),
    odoo_bin: str = typer.Option(
        None, "--odoo-bin",
        help="Path to the source odoo-bin: records Odoo + Python versions into "
             "the bundle's provenance.json.",
    ),
) -> None:
    """Self-hosted path: dump, mask on a scratch DB, and package the result."""
    import os
    from .core import mask, package
    from .core.package import PackageConfig
    from .adapters import self_hosted
    from .adapters.self_hosted import DumpConfig
    # 1. Validate the rulebook up front -- cheap, catches typos first.
    try:
        rb = load_and_validate(rules)
    except RulebookError as exc:
        _fail(f"rulebook invalid: {exc}")
        return
    # 2. Resolve URLs. SCRATCH_DB_URL is the masking target; SOURCE_DB_URL
    #    is what we dump. Guardrail: we NEVER mask the source in place.
    scratch_url = scratch_db_url or os.environ.get("SCRATCH_DB_URL")
    if not scratch_url:
        _fail("no scratch DB URL: pass --scratch-db-url or set SCRATCH_DB_URL "
              "(postgres-anon service from docker/docker-compose.scratch.yml)")
        return
    source_url = source_db_url or os.environ.get("SOURCE_DB_URL")
    if not source_url:
        _fail("no source DB URL: pass --source-db-url or set SOURCE_DB_URL "
              "(the Odoo DB to snapshot -- never masked in place)")
        return
    out_path = Path(out)
    out_path.mkdir(parents=True, exist_ok=True)
    # 3. Dump source -> bundle layout (filestore handled by the adapter).
    typer.secho(f"dumping source {db} -> {out_path} ...", fg=typer.colors.CYAN)
    try:
        self_hosted.dump(DumpConfig(
            db_url=source_url, db_name=db, filestore_dir=filestore_dir,
            drop_attachment_content=True,
        ), out_path)
    except self_hosted.SelfHostedError as exc:
        _fail(f"dump failed: {exc}")
        return
    # 4. Restore the dump into the scratch DB (we mask on scratch, never source).
    typer.secho(f"restoring dump into scratch DB ...", fg=typer.colors.CYAN)
    try:
        _restore_into_scratch(out_path, scratch_url, db)
    except Exception as exc:
        _fail(f"restore into scratch failed: {exc}")
        return
    # 5. Load bootstrap functions (anon + odoo_synth helpers) into scratch.
    typer.secho("loading odoo_synth bootstrap into scratch DB ...", fg=typer.colors.CYAN)
    try:
        _load_bootstrap(scratch_url)
    except Exception as exc:
        _fail(f"bootstrap load failed: {exc}")
        return
    # 6. Mask.
    typer.secho("masking scratch DB ...", fg=typer.colors.CYAN)
    try:
        summary = mask.apply_masking(scratch_url, rb)
    except mask.MaskError as exc:
        _fail(f"masking failed: {exc}")
        return
    arch = summary.get("scoped_arch_replace") or {}
    typer.secho(
        f"masked: {summary['labels_applied']} labels applied "
        f"({summary['labels_skipped']} skipped), "
        f"{summary.get('pattern_applied', 0)} pattern labels applied "
        f"({summary.get('pattern_skipped', 0)} skipped), "
        f"{summary['shuffle_applied']} shuffles, "
        f"{summary['rotate_applied']} rotations, "
        f"arch_db views rewritten: {arch.get('views_touched', 0)} "
        f"({arch.get('rows_updated', 0)} rows, {arch.get('names_replaced', 0)} names), "
        f"attachment rows scrubbed: {summary['attachment'].get('rows_content_dropped', 0)}",
        fg=typer.colors.CYAN,
    )
    # 7. Package.
    typer.secho(f"packaging masked DB -> {out_path} ...", fg=typer.colors.CYAN)
    try:
        package.package(PackageConfig(
            db_url=scratch_url, out=out_path,
            rulebook_dir=Path(rules),
        ), rb)
    except package.PackageError as exc:
        _fail(f"packaging failed: {exc}")
        return

    # 8. Provenance sidecar: record the source's non-data layers (Odoo/PG/
    #    Python versions, installed modules, addon code presence) so a replica
    #    can reproduce the environment, not just the data. Detected from the
    #    SOURCE (never masked), written next to db.dump. Non-fatal: a bundle is
    #    still usable for `up` without it; it's required for `replica`.
    from .core import provenance
    try:
        prov = provenance.detect(
            source_url, odoo_conf=odoo_conf, odoo_bin=odoo_bin,
        )
        (out_path / "provenance.json").write_text(prov.to_json(), "utf-8")
        note = ""
        if prov.missing_addons:
            note = (f" -- WARNING: {len(prov.missing_addons)} installed module(s) "
                    f"have no code on addons_path (see provenance.json)")
        typer.secho(
            f"provenance: Odoo {prov.odoo_series}, PostgreSQL {prov.postgres_major}, "
            f"{prov.installed_module_count} installed modules{note}",
            fg=typer.colors.YELLOW if prov.missing_addons else typer.colors.CYAN,
        )
    except provenance.ProvenanceError as exc:
        # Detection is best-effort; don't fail a good snapshot over it.
        typer.secho(f"provenance detection skipped: {exc}", fg=typer.colors.YELLOW)

    typer.secho(f"OK: snapshot written to {out_path}", fg=typer.colors.GREEN)


@app.command("up")
def up(
    from_: str = typer.Option(..., "--from", help="Path to a masked bundle to provision."),
    db: str = typer.Option("masked_odoo", "--db", help="Name for the fresh provisioned DB."),
    db_url: str = typer.Option(None, "--db-url",
        help="psycopg URL to a Postgres where the fresh DB will be created."),
    no_launch: bool = typer.Option(False, "--no-launch", help="Restore + neutralize only, don't start Odoo."),
    set_admin_password: str = typer.Option(
        None, "--set-admin-password",
        help="Reset the admin user's password to this value so the masked "
             "instance is immediately loginable (logins/passwords are masked, "
             "so otherwise there's no known credential to get in).",
    ),
) -> None:
    """Provision a fresh Odoo instance from a masked bundle (with --neutralize)."""
    import os
    from .core import provision
    from .core.provision import ProvisionConfig
    url = db_url or os.environ.get("PROVISION_DB_URL") or os.environ.get("SCRATCH_DB_URL")
    if not url:
        _fail("no db-url: pass --db-url or set PROVISION_DB_URL/SCRATCH_DB_URL")
        return
    try:
        report = provision.provision(ProvisionConfig(
            bundle=Path(from_), db_name=db, db_url=url,
            launch=not no_launch,
            set_admin_password=set_admin_password,
        ))
    except provision.ProvisionError as exc:
        _fail(f"provision failed: {exc}")
        return
    login_note = ""
    if report.get("admin_login"):
        login_note = f", admin_login={report['admin_login']!r}"
    typer.secho(
        f"OK: provisioned {report['db_name']} (neutralized={report['neutralized']}, "
        f"launched={report['launched']}, creds_verified={report['credential_verification']['passed']}"
        f"{login_note})",
        fg=typer.colors.GREEN,
    )


# ---------------------------------------------------------------------------
# replica  (generate a portable native replica kit from a masked bundle)
# ---------------------------------------------------------------------------


@app.command("replica")
def replica(
    from_: str = typer.Option(..., "--from", help="Path to a masked bundle (must contain provenance.json)."),
    out: str = typer.Option(..., "--out", help="Directory to write the generated replica kit into."),
    # Target Odoo runtime.
    addons_path: str = typer.Option(
        "/opt/odoo/odoo/addons", "--addons-path",
        help="Colon-separated addons_path on the TARGET (preflight verifies module code here)."),
    odoo_bin: str = typer.Option("/opt/odoo/odoo/odoo-bin", "--odoo-bin", help="Path to odoo-bin on the target."),
    python_bin: str = typer.Option("python3", "--python", help="Python interpreter on the target."),
    # Target PostgreSQL.
    db_host: str = typer.Option("", "--db-host", help="Target PG host (empty => local socket / peer auth)."),
    db_port: int = typer.Option(5432, "--db-port", help="Target PG port."),
    db_user: str = typer.Option("odoo", "--db-user", help="Target PG role."),
    db_password: str = typer.Option("", "--db-password", help="Target PG password (empty => socket/.pgpass)."),
    db_name: str = typer.Option("odoo", "--db-name", help="Name for the replica DB on the target."),
    # Target Odoo service.
    data_dir: str = typer.Option("/var/lib/odoo", "--data-dir", help="Odoo data_dir on the target."),
    http_port: int = typer.Option(8069, "--http-port", help="HTTP port for the replica."),
    admin_password: str = typer.Option("admin", "--admin-password", help="Admin login password to set post-restore."),
    master_password: str = typer.Option("", "--master-password", help="odoo.conf admin_passwd (DB-management master)."),
    service_name: str = typer.Option("odoo", "--service-name", help="systemd service name."),
    service_user: str = typer.Option("odoo", "--service-user", help="System user the service runs as."),
    allow_mismatch: bool = typer.Option(
        False, "--allow-mismatch",
        help="Downgrade PG-major / Odoo-series skew from hard-fail to warning "
             "in preflight. Missing addon code stays a hard failure."),
) -> None:
    """Generate a portable native replica kit (preflight + install scripts,
    odoo.conf, systemd unit) from a masked bundle, ready to run on a target."""
    from .core import replica as replica_mod
    from .core.replica import ReplicaConfig
    cfg = ReplicaConfig(
        addons_path=addons_path, odoo_bin=odoo_bin, python_bin=python_bin,
        db_host=db_host, db_port=db_port, db_user=db_user,
        db_password=db_password, db_name=db_name,
        data_dir=data_dir, http_port=http_port,
        admin_password=admin_password, master_password=master_password,
        service_name=service_name, service_user=service_user,
        allow_mismatch=allow_mismatch,
    )
    try:
        kit = replica_mod.generate_kit(Path(from_), Path(out), cfg)
    except replica_mod.ReplicaError as exc:
        _fail(f"replica generation failed: {exc}")
        return
    typer.secho(f"OK: replica kit written to {kit}", fg=typer.colors.GREEN)
    typer.echo("  Next: copy the bundle's db.dump (and filestore/) beside the kit, then on the target run:")
    typer.echo(f"    ./preflight.sh   &&   sudo ./install.sh")


# ---------------------------------------------------------------------------
# snapshot helpers (dump -> restore-into-scratch -> bootstrap -> mask -> package)
# ---------------------------------------------------------------------------


def _restore_into_scratch(bundle: Path, scratch_url: str, db_name: str) -> None:
    """Restore the dumped bundle into the scratch DB (we mask on scratch).

    The scratch DB must already have the anon extension. We restore the
    public-schema tables from the bundle's dump.sql (odoo-bin path) or
    pg_restore the dump/ dir.
    """
    import psycopg
    from .adapters import self_hosted
    # Reuse the adapter's load() but pointed at scratch, not a fresh DB.
    # We don't go through odoo-bin db load here (that would neutralize the
    # SOURCE, which we don't want before masking). Instead plain restore.
    # Drop public-schema Odoo tables first so re-runs are clean, preserving
    # the anon extension (which lives in public per the extension's
    # extnamespace -- see core/mask.py notes).
    with psycopg.connect(scratch_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DO $$ DECLARE r record; BEGIN "
                "FOR r IN SELECT relname FROM pg_class c "
                "JOIN pg_namespace n ON c.relnamespace=n.oid "
                "WHERE n.nspname='public' AND c.relkind='r' "
                "AND relname NOT IN ('anon') LOOP "
                "EXECUTE format('DROP TABLE IF EXISTS %I CASCADE', r.relname); "
                "END LOOP; END $$;"
            )
    # Custom-format dump (db.dump) -> pg_restore (via pgtools, which
    # auto-falls back to the scratch container's own pg_restore on a
    # major-version mismatch -- see core/pgtools.py). Plain SQL (dump.sql,
    # from the odoo-bin path) -> psql directly.
    from .core import pgtools
    import os
    dump = bundle / "db.dump"
    if dump.exists():
        # The restore subprocess may run in-container (auto or via the
        # explicit ODOO_SYNTH_PG_RESTORE override); honor
        # ODOO_SYNTH_RESTORE_DB_URL for the in-container socket form when set
        # explicitly (separate from ODOO_SYNTH_DUMP_DB_URL because dump
        # targets the source and restore targets the scratch -- different
        # DBs).
        restore_url = os.environ.get("ODOO_SYNTH_RESTORE_DB_URL", scratch_url)
        with open(dump, "rb") as dump_fh:
            try:
                proc = pgtools.run_pg_tool(
                    "pg_restore",
                    ["--no-owner", "--no-privileges", "--clean", "--if-exists"],
                    restore_url,
                    cmd_env="ODOO_SYNTH_PG_RESTORE",
                    url_envs=("ODOO_SYNTH_RESTORE_DB_URL",),
                    input_file=dump_fh,
                    url_as_flag="-d",
                    # pg_restore --clean --if-exists routinely exits 1 with
                    # benign "errors ignored on restore" notices (e.g. DROP
                    # EXTENSION on an object something else still depends on);
                    # we verify success ourselves below (res_partner exists),
                    # not the return code -- matches the pre-existing
                    # behavior this module replaced.
                    tolerate_nonzero_exit=True,
                )
            except pgtools.PgToolError as exc:
                raise RuntimeError(f"pg_restore into scratch failed: {exc}") from exc
        # Verify the restore actually loaded data -- pg_restore --clean emits
        # benign "does not exist" notices on a fresh DB, so return-code alone
        # is misleading. We check res_partner exists in the scratch DB; if not,
        # the restore no-op'd (wrong DB, empty dump) -- a real failure.
        with psycopg.connect(scratch_url, autocommit=True) as chk:
            with chk.cursor() as c:
                c.execute("SELECT to_regclass('public.res_partner') IS NOT NULL")
                has_partner = c.fetchone()[0]
        if not has_partner:
            raise RuntimeError(
                f"pg_restore did not load data into {scratch_url} "
                f"(res_partner absent). pg_restore stderr: {proc.stderr}"
            )
        return
    sql = bundle / "dump.sql"
    if sql.exists():
        with psycopg.connect(scratch_url, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(sql.read_text("utf-8"))
        return
    raise RuntimeError(f"bundle {bundle} has no db.dump or dump.sql to restore")




def _load_bootstrap(scratch_url: str) -> None:
    """Load sql/bootstrap.sql (anon + odoo_synth functions) into scratch."""
    import psycopg
    from pathlib import Path
    bootstrap = Path(__file__).resolve().parents[1] / "sql" / "bootstrap.sql"
    with psycopg.connect(scratch_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(bootstrap.read_text("utf-8"))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
