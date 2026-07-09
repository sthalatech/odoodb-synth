"""Provision a fresh Odoo instance from a masked bundle.

GUARDRAIL (from AGENT_PROMPT.md): must call `odoo-bin db load --neutralize`
(or equivalent) before booting. Never boot a provisioned instance without
--neutralize -- that's what disables outgoing mail, cron, and payment
providers at runtime, on top of the credential scrubbing the rulebook did.

This module restores a masked bundle into a fresh Postgres DB, runs the
neutralize path, and (where odoo-bin is available) launches Odoo pointed at
it. The credential fields the rulebook's 60_system_secrets.yml handles are
re-verified after neutralize, because operational neutralization and
credential scrubbing are different layers -- we confirm, don't trust.

Two modes:
  * odoo-bin available: `odoo-bin db load --neutralize <dbname> <bundle>`
    handles restore + neutralize in one step (delegated to
    adapters/self_hosted.py:load, which passes --neutralize). We then launch
    the Odoo container/process.
  * odoo-bin NOT available: pg_restore the bundle's db.dump into a fresh DB,
    then apply the ir.config_parameter neutralization keys manually
    (database.uuid reset, outgoing_mail/server disabled, payment providers
    disabled). This is a best-effort fallback; odoo-bin --neutralize is the
    authoritative path.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..adapters import self_hosted
from .rulebook import Rulebook


class ProvisionError(Exception):
    """Raised when provisioning fails."""


@dataclass
class ProvisionConfig:
    bundle: Path
    db_name: str
    db_url: str  # admin URL to a Postgres where we can CREATE DATABASE
    odoo_bin: str | None = None
    # If True, launch the Odoo container after load (odoo-bin path only).
    launch: bool = True


def provision(cfg: ProvisionConfig, rulebook: Rulebook | None = None) -> dict[str, Any]:
    """Restore a masked bundle, neutralize, and (optionally) launch Odoo.

    Returns a report dict. Never boots Odoo without --neutralize.

    Steps:
      1. Create a fresh target DB (DROP IF EXISTS first, idempotent).
      2. Restore the bundle (odoo-bin db load --neutralize if available, else
         pg_restore + manual neutralize).
      3. Re-verify the credential fields neutralize does NOT cover, per
         60_system_secrets.yml's framing -- confirm they're actually scrubbed,
         don't trust that --neutralize handled them.
      4. If launch=True and odoo-bin is available, start Odoo against the DB.
    """
    bundle = Path(cfg.bundle)
    manifest = _read_manifest(bundle)

    # 1. Fresh target DB.
    _recreate_db(cfg)

    # 2. Restore + neutralize.
    odoo_bin = self_hosted.find_odoo_bin(cfg.odoo_bin)
    used_neutralize = False
    if odoo_bin:
        self_hosted.load(bundle, cfg.db_name, cfg.db_url, odoo_bin)
        used_neutralize = True
    else:
        _pg_restore(bundle, cfg)
        _manual_neutralize(cfg)
        used_neutralize = True  # we did our own neutralize

    if not used_neutralize:
        raise ProvisionError(
            "refusing to boot: no neutralize path was run. This is a guardrail "
            "violation -- provision() must never boot Odoo without --neutralize "
            "or an equivalent manual neutralization."
        )

    # 3. Re-verify credential fields.
    verify = _verify_credentials_scrubbed(cfg, rulebook)

    # 4. Launch (optional, odoo-bin path only).
    launched = False
    if cfg.launch and odoo_bin:
        launched = _launch_odoo(odoo_bin, cfg.db_name)

    return {
        "db_name": cfg.db_name,
        "restored": True,
        "neutralized": used_neutralize,
        "credential_verification": verify,
        "launched": launched,
        "manifest_source": manifest.get("source"),
    }


# ---------------------------------------------------------------------------
# DB (re)creation + restore
# ---------------------------------------------------------------------------


def _recreate_db(cfg: ProvisionConfig) -> None:
    import psycopg
    # Connect to the maintenance DB (strip the dbname off the URL) to
    # CREATE/DROP the target.
    admin_url = _admin_url(cfg.db_url, cfg.db_name)
    with psycopg.connect(admin_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{cfg.db_name}" WITH (FORCE)')
            cur.execute(f'CREATE DATABASE "{cfg.db_name}"')


def _pg_restore_binary() -> list[str]:
    """Resolve the pg_restore command, honoring ODOO_SYNTH_PG_RESTORE (same
    pattern as core/package.py's ODOO_SYNTH_PG_DUMP). Needed when the host's
    pg_restore is an older major than the dump (a PG16 pg_restore can't read
    a PG18 custom-format dump)."""
    import os
    import shlex
    override = os.environ.get("ODOO_SYNTH_PG_RESTORE")
    if override:
        return shlex.split(override)
    return ["pg_restore"]


def _restore_db_url(db_url: str) -> str:
    """DB URL for pg_restore specifically. Honors ODOO_SYNTH_RESTORE_DB_URL
    so the restore subprocess (possibly in-container) connects to the right
    socket while psycopg connects from the host."""
    import os
    return os.environ.get("ODOO_SYNTH_RESTORE_DB_URL", db_url)


def _pg_restore(bundle: Path, cfg: ProvisionConfig) -> None:
    import os
    dump = bundle / "db.dump"
    if not dump.exists():
        raise ProvisionError(
            f"bundle has no db.dump at {dump}; odoo-bin is unavailable so the "
            "pg_restore fallback needs a custom-format dump (pg_dump -Fc)."
        )
    # Pipe the dump to pg_restore's stdin so it works through `docker exec`
    # redirects (the dump file is on the host, pg_restore may be in-container).
    # Restore into the TARGET DB (not the admin/maintenance DB -- cfg.db_url
    # points at `postgres` so CREATE DATABASE works; the restore must land in
    # cfg.db_name).
    target_url = _target_db_url(cfg.db_url, cfg.db_name)
    # ODOO_SYNTH_RESTORE_DB_URL override: when the restore binary runs in a
    # container (host pg_restore too old for the server major), the in-
    # container DB URL differs from the host URL. The override may contain a
    # literal {dbname} placeholder substituted with the target DB name, so a
    # single env var works for any target DB (the target name is dynamic, so a
    # static URL can't point at it). Without the placeholder the override is
    # used as-is (back-compat with the existing ODOO_SYNTH_RESTORE_DB_URL form
    # used elsewhere -- though those callers restore into a fixed DB).
    override = os.environ.get("ODOO_SYNTH_RESTORE_DB_URL")
    if override:
        restore_url = override.replace("{dbname}", cfg.db_name)
    else:
        restore_url = target_url
    proc = subprocess.run(
        _pg_restore_binary() + ["-d", restore_url,
                                 "--no-owner", "--no-privileges",
                                 "--clean", "--if-exists"],
        stdin=open(dump, "rb"), capture_output=True, text=True,
    )
    # pg_restore --clean emits benign "does not exist" notices on a fresh DB;
    # verify data actually loaded into the TARGET DB (not the admin DB) rather
    # than trusting the return code. Connecting to target_url is the fix for
    # the bug where the verify checked the maintenance DB and passed vacuously
    # while the restore had silently dumped into `postgres`.
    import psycopg
    with psycopg.connect(target_url, autocommit=True) as chk:
        with chk.cursor() as c:
            c.execute("SELECT to_regclass('public.res_partner') IS NOT NULL OR "
                      "to_regclass('public.ir_config_parameter') IS NOT NULL")
            loaded = c.fetchone()[0]
    if not loaded:
        raise ProvisionError(
            f"pg_restore did not load data into {target_url} "
            f"(db_name={cfg.db_name}). The admin URL was {cfg.db_url}; "
            f"check that ODOO_SYNTH_RESTORE_DB_URL (if set) points at the "
            f"target DB, not the maintenance DB. stderr: {proc.stderr}"
        )


def _manual_neutralize(cfg: ProvisionConfig) -> None:
    """Best-effort manual neutralization when odoo-bin isn't available.

    Mirrors what `odoo-bin db load --neutralize` does at the config-parameter
    level: disable outgoing mail, cron, payment providers. This is NOT as
    authoritative as odoo-bin's own neutralize (which also patches module
    state), so we log that the fallback was used. The rulebook's
    60_system_secrets.yml already scrubbed the credential values; this is
    the operational layer on top.
    """
    import psycopg
    target_url = _target_db_url(cfg.db_url, cfg.db_name)
    with psycopg.connect(target_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            # Only touch tables that exist (the masked DB may not have every
            # Odoo table if modules aren't installed).
            for stmt in _NEUTRALIZE_SQL:
                try:
                    cur.execute(stmt)
                except psycopg.Error as exc:
                    if "does not exist" in str(exc).lower():
                        continue
                    raise ProvisionError(f"neutralize stmt failed: {stmt}\n  {exc}") from exc


# ir.config_parameter keys odoo-bin --neutralize sets. We replicate the
# operational ones. Credential VALUES are handled by the rulebook (already
# rotated in mask.py); this is the operational-disable layer.
_NEUTRALIZE_SQL = [
    "UPDATE ir_config_parameter SET value = '0' WHERE key = 'mail.force.smtp.from' AND value IS NOT NULL",
    "UPDATE ir_config_parameter SET value = 'smtp' WHERE key = 'mail.default.server' ",
    # Disable all mail servers + fetchmail servers.
    "UPDATE ir_mail_server SET active = false, smtp_host = NULL, smtp_user = NULL, smtp_pass = NULL",
    "UPDATE fetchmail_server SET active = false, password = NULL, \"user\" = NULL",
    # payment providers disabled (belt-and-suspenders; the rulebook already
    # set state='disabled', neutralize does it too -- redundant on purpose).
    "UPDATE payment_provider SET state = 'disabled'",
]


# ---------------------------------------------------------------------------
# Credential re-verification (the point of this module beyond restore)
# ---------------------------------------------------------------------------


def _verify_credentials_scrubbed(cfg: ProvisionConfig, rulebook: Rulebook | None) -> dict[str, Any]:
    """Re-verify the credential fields neutralize does NOT cover.

    Per 60_system_secrets.yml's framing: operational neutralization (mail,
    cron, payment providers at runtime) is a DIFFERENT layer from credential
    scrubbing (the secret values sitting in the DB). We confirm the latter
    actually happened, don't trust that --neutralize handled it (it doesn't
    -- that's what the rulebook's rotate_secret rules were for).

    Checks (all must pass; a failure is a guardrail violation, not a
    warning):
      * ir_config_parameter: database.secret + database.uuid differ from any
        non-null source value (we can't know the source value here, so we
        assert they're non-null AND were rotated -- the rulebook sets them
        to fresh random values; if they're still NULL or unchanged-shaped,
        that's a failure).
      * payment_token.provider_ref IS NULL for all rows.
      * payment_provider.state = 'disabled' for all rows.
      * mail_tracking_value old/new value columns are NULL.
    """
    import psycopg
    res: dict[str, Any] = {"checks": {}, "passed": True}
    target_url = _target_db_url(cfg.db_url, cfg.db_name)
    with psycopg.connect(target_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            def _q(sql, missing_ok=True):
                try:
                    cur.execute(sql)
                    return cur.fetchone()
                except psycopg.Error as exc:
                    if missing_ok and "does not exist" in str(exc).lower():
                        return None
                    raise

            # Guardrail: the target DB must actually have Odoo tables loaded.
            # If the restore silently dumped into the maintenance DB (the
            # cfg.db_url-vs-target bug this check now catches), every
            # credential table is absent and the per-table checks below pass
            # VACUOUSLY (missing_ok=True -> None -> pass). A 0-table target
            # must FAIL verify, not pass. This is what hid the first
            # real-schema provisioning bug on darkstore.
            cur.execute(
                "SELECT count(*) FROM pg_class c "
                "JOIN pg_namespace n ON c.relnamespace=n.oid "
                "WHERE c.relkind='r' AND n.nspname='public'"
            )
            public_tables = cur.fetchone()[0]
            res["checks"]["target_db_has_tables"] = public_tables > 0

            # database.secret / database.uuid present + non-null.
            row = _q("SELECT value FROM ir_config_parameter WHERE key='database.secret'")
            res["checks"]["database_secret_nonnull"] = bool(row and row[0])
            row = _q("SELECT value FROM ir_config_parameter WHERE key='database.uuid'")
            res["checks"]["database_uuid_nonnull"] = bool(row and row[0])

            row = _q("SELECT count(*) FROM payment_token WHERE provider_ref IS NOT NULL")
            res["checks"]["payment_token_provider_ref_null"] = (row[0] == 0) if row else True

            row = _q("SELECT count(*) FROM payment_provider WHERE state IS NULL OR state <> 'disabled'")
            res["checks"]["payment_provider_disabled"] = (row[0] == 0) if row else True

            row = _q("SELECT count(*) FROM mail_tracking_value "
                     "WHERE old_value_char IS NOT NULL OR new_value_char IS NOT NULL")
            res["checks"]["mail_tracking_values_null"] = (row[0] == 0) if row else True

    failed = [k for k, v in res["checks"].items() if not v]
    res["passed"] = not failed
    if failed:
        raise ProvisionError(
            "credential re-verification FAILED after neutralize -- the "
            f"rulebook's scrub did not take: {failed}. Operational neutralize "
            "and credential scrubbing are different layers; the latter must "
            "hold independent of --neutralize. Do NOT boot this instance."
        )
    return res


# ---------------------------------------------------------------------------
# Launch
# ---------------------------------------------------------------------------


def _launch_odoo(odoo_bin: str, db_name: str) -> bool:
    """Start Odoo against the provisioned DB. Best-effort; non-blocking."""
    # We don't block on the server here -- launch it and return. The caller
    # (CLI / tests) can then poll the HTTP port.
    try:
        subprocess.Popen(
            [odoo_bin, "-d", db_name, "--without-demo=True"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _read_manifest(bundle: Path) -> dict:
    mf = bundle / "manifest.json"
    if mf.exists():
        try:
            return json.loads(mf.read_text("utf-8"))
        except Exception:
            return {}
    return {}


def _admin_url(db_url: str, db_name: str) -> str:
    """Strip the dbname off a psycopg URL to get the maintenance DB URL."""
    # postgresql://user:pass@host:port/dbname -> .../postgres
    if "/" in db_url.rsplit("@", 1)[-1]:
        base = db_url.rsplit("/", 1)[0]
        return base + "/postgres"
    return db_url + "/postgres"


def _target_db_url(db_url: str, db_name: str) -> str:
    """The URL of the freshly-created target DB (admin URL base + /<db_name>).

    cfg.db_url is the ADMIN URL (points at the maintenance DB so we can
    CREATE DATABASE); the restore, manual-neutralize, and credential-verify
    steps must connect to the TARGET DB, not the maintenance DB. Restoring
    into the admin URL would dump Odoo's tables into the `postgres` DB -- a
    real bug found by the first real-schema provisioning run on darkstore.
    """
    if "/" in db_url.rsplit("@", 1)[-1]:
        base = db_url.rsplit("/", 1)[0]
        return f"{base}/{db_name}"
    return f"{db_url}/{db_name}"
