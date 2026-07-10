"""Generate a self-contained native "replica kit" from a masked bundle.

The bundle produced by ``odoo-synth snapshot`` carries the *data* layer (a
masked ``db.dump``), a ``provenance.json`` describing the source's non-data
layers (Odoo series, PostgreSQL major, installed module set, addon code), and
an (optional) ``filestore/`` tree. This module turns that bundle plus a set of
*target* parameters into a portable directory of shell/SQL/config artifacts
that reproduce a running Odoo replica on a fresh server -- with no Docker at
runtime and without odoo-synth itself being present on the target (decision
C-1 option 2: emit a portable kit + script, we do not SSH-drive the target).

What the generated kit contains:

  * ``preflight.sh`` -- verifies the target *before* touching anything: the
    PostgreSQL major version matches provenance (hard fail on mismatch unless
    ``--allow-mismatch``), the target ``odoo-bin`` reports the same Odoo
    series (hard fail on major mismatch), a Python interpreter is present, and
    -- critically -- every module the source had *installed* has its code on
    the target's ``addons_path`` (decision A3: verify, don't bundle). A
    missing installed module is always a hard failure: Odoo will not boot
    without it.

  * ``install.sh`` -- runs preflight, then reproduces provision.py's proven
    restore path as shell + SQL: create the target DB, pin its ``search_path``
    to ``public`` (the fix for odoo_synth's schema shadowing), ``pg_restore``
    the masked ``db.dump`` (``--no-owner --no-privileges --clean --if-exists``,
    tolerating the benign non-zero exit pg_restore returns on ``--clean`` of a
    fresh DB), copy the filestore, drop in ``odoo.conf``, apply the neutralize
    SQL (disable mail/fetchmail/payment providers), set the admin password
    (pbkdf2-sha512, matching Odoo's ``passlib`` scheme), install + start the
    systemd unit, and finally HTTP health-check the running server.

  * ``odoo.conf`` -- rendered for the target (addons_path, db params, data_dir,
    http_port, ``proxy_mode``/``list_db`` hardening).

  * ``<service>.service`` -- a systemd unit to run odoo-bin as the service user.

  * ``provenance.json`` -- copied verbatim so the target retains the record.

  * ``README.md`` -- operator instructions.

Generation is *pure*: this module only reads the bundle and writes files. It
never connects to the target or executes the kit -- that is the operator's
step, on the target box, per the portable-kit decision.
"""

from __future__ import annotations

import json
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path

from .provenance import ProvenanceReport


class ReplicaError(Exception):
    """Raised for hard failures while generating a replica kit.

    Examples: the source bundle has no ``provenance.json`` (we cannot verify
    the target without it), or the bundle's primary dump artifact is missing.
    """


# ---------------------------------------------------------------------------
# Target configuration
# ---------------------------------------------------------------------------


@dataclass
class ReplicaConfig:
    """Everything about the *target* that the generated kit needs baked in.

    None of these touch the source -- they describe where and how the replica
    should run once the operator executes the kit on the target box.
    """

    # --- Target Odoo runtime ---
    addons_path: str = "/opt/odoo/odoo/addons"
    odoo_bin: str = "/opt/odoo/odoo/odoo-bin"
    python_bin: str = "python3"

    # --- Target PostgreSQL ---
    db_host: str = ""  # empty => local unix socket / peer auth
    db_port: int = 5432
    db_user: str = "odoo"
    db_password: str = ""  # empty => rely on socket peer / .pgpass
    db_name: str = "odoo"

    # --- Target Odoo service ---
    data_dir: str = "/var/lib/odoo"
    http_port: int = 8069
    admin_password: str = "admin"  # login password set post-restore
    master_password: str = ""  # odoo.conf admin_passwd (DB-management)
    service_name: str = "odoo"
    service_user: str = "odoo"

    # --- Version-tolerance policy (decision E) ---
    # When True, preflight downgrades a PG-major / Odoo-series mismatch from a
    # hard failure to a loud warning. Missing addon code is ALWAYS hard.
    allow_mismatch: bool = False


# ---------------------------------------------------------------------------
# Bundle names (mirror package.py / snapshot output)
# ---------------------------------------------------------------------------

_PROVENANCE_NAME = "provenance.json"
_DUMP_NAME = "db.dump"
_FILESTORE_DIR = "filestore"


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def generate_kit(bundle: str | Path, out_dir: str | Path, cfg: ReplicaConfig) -> Path:
    """Generate a replica kit from ``bundle`` into ``out_dir``.

    Reads ``provenance.json`` from the bundle (required) and emits a portable
    directory of scripts + config that, when run on the target, restores the
    masked data and brings up a running Odoo replica.

    Returns the resolved output directory path.

    Raises :class:`ReplicaError` if the bundle lacks ``provenance.json`` or the
    primary dump artifact.
    """
    bundle_path = Path(bundle)
    out_path = Path(out_dir)

    prov_file = bundle_path / _PROVENANCE_NAME
    if not prov_file.is_file():
        raise ReplicaError(
            f"bundle has no {_PROVENANCE_NAME} ({prov_file}); re-run "
            "'odoo-synth snapshot' with --odoo-conf/--odoo-bin so the "
            "replica can verify the target."
        )
    dump_file = bundle_path / _DUMP_NAME
    if not dump_file.is_file():
        raise ReplicaError(f"bundle has no {_DUMP_NAME} ({dump_file}); nothing to restore.")

    try:
        report = ProvenanceReport.from_dict(json.loads(prov_file.read_text("utf-8")))
    except (ValueError, TypeError) as exc:
        raise ReplicaError(f"could not parse {prov_file}: {exc}") from exc

    out_path.mkdir(parents=True, exist_ok=True)

    # Copy provenance verbatim so the target retains the record.
    shutil.copyfile(prov_file, out_path / _PROVENANCE_NAME)

    _write(out_path / "preflight.sh", _render_preflight(report, cfg), executable=True)
    _write(out_path / "install.sh", _render_install(report, cfg), executable=True)
    _write(out_path / "odoo.conf", _render_odoo_conf(report, cfg))
    _write(out_path / f"{cfg.service_name}.service", _render_systemd(cfg))
    _write(out_path / "README.md", _render_readme(report, cfg))

    return out_path


# ---------------------------------------------------------------------------
# File-writing helper
# ---------------------------------------------------------------------------


def _write(path: Path, content: str, *, executable: bool = False) -> None:
    path.write_text(content, "utf-8")
    if executable:
        mode = path.stat().st_mode
        path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _sh_quote(value: str) -> str:
    """Single-quote a value for safe embedding in generated bash."""
    return "'" + value.replace("'", "'\\''") + "'"


# ---------------------------------------------------------------------------
# preflight.sh
# ---------------------------------------------------------------------------


def _render_preflight(report: ProvenanceReport, cfg: ReplicaConfig) -> str:
    modules = " ".join(m.name for m in report.installed_modules) or ""
    # A missing installed module is always fatal; version skew is fatal unless
    # allow_mismatch flips it to a warning.
    mismatch_action = "warn" if cfg.allow_mismatch else "fail"

    return f"""#!/usr/bin/env bash
# preflight.sh -- verify this target can host the source replica.
# Generated by odoo-synth. Reads nothing from the source; checks the target.
#
# Exit non-zero (and stop) on any hard failure. With --allow-mismatch baked in
# at generation time, PG-major / Odoo-series skew is downgraded to a warning;
# MISSING ADDON CODE is always fatal (Odoo cannot boot without it).
set -euo pipefail

EXPECTED_PG_MAJOR={report.postgres_major}
EXPECTED_ODOO_SERIES={_sh_quote(report.odoo_series)}
MISMATCH_ACTION={_sh_quote(mismatch_action)}
ODOO_BIN={_sh_quote(cfg.odoo_bin)}
PYTHON_BIN={_sh_quote(cfg.python_bin)}
ADDONS_PATH={_sh_quote(cfg.addons_path)}
INSTALLED_MODULES={_sh_quote(modules)}

fail() {{ echo "PREFLIGHT FAIL: $*" >&2; exit 1; }}
warn() {{ echo "PREFLIGHT WARN: $*" >&2; }}
skew() {{ if [ "$MISMATCH_ACTION" = fail ]; then fail "$*"; else warn "$* (allowed by --allow-mismatch)"; fi }}
ok()   {{ echo "PREFLIGHT OK:   $*"; }}

echo "== odoo-synth replica preflight =="

# --- PostgreSQL major version ---
if ! command -v psql >/dev/null 2>&1; then
  fail "psql not found on PATH (need PostgreSQL client tools)."
fi
PG_MAJOR="$(psql -X -A -t -c 'SHOW server_version_num;' 2>/dev/null | head -c3 | sed 's/0*$//')"
# server_version_num is e.g. 160014 => major 16. Extract robustly:
PG_MAJOR="$(psql -X -A -t -c 'SELECT current_setting(''server_version_num'')::int / 10000;' 2>/dev/null || true)"
if [ -z "$PG_MAJOR" ]; then
  skew "could not determine target PostgreSQL major version (is the server reachable?)"
elif [ "$PG_MAJOR" != "$EXPECTED_PG_MAJOR" ]; then
  skew "PostgreSQL major $PG_MAJOR != source $EXPECTED_PG_MAJOR"
else
  ok "PostgreSQL major $PG_MAJOR matches source."
fi

# --- Odoo series ---
if [ ! -x "$ODOO_BIN" ] && ! command -v "$ODOO_BIN" >/dev/null 2>&1; then
  fail "odoo-bin not found or not executable: $ODOO_BIN"
fi
ODOO_VER_RAW="$("$ODOO_BIN" --version 2>/dev/null || true)"
# e.g. "Odoo Server 19.0" -> series 19.0
TARGET_SERIES="$(echo "$ODOO_VER_RAW" | grep -oE '[0-9]+\\.[0-9]+' | head -n1 || true)"
if [ -z "$TARGET_SERIES" ]; then
  skew "could not determine target Odoo series from: $ODOO_VER_RAW"
elif [ "$TARGET_SERIES" != "$EXPECTED_ODOO_SERIES" ]; then
  skew "Odoo series $TARGET_SERIES != source $EXPECTED_ODOO_SERIES"
else
  ok "Odoo series $TARGET_SERIES matches source."
fi

# --- Python interpreter ---
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  fail "Python interpreter not found: $PYTHON_BIN"
fi
ok "Python present: $("$PYTHON_BIN" --version 2>&1)"

# --- Installed module code present on addons_path (ALWAYS hard) ---
MISSING=""
IFS=':' read -r -a AP <<< "$ADDONS_PATH"
for mod in $INSTALLED_MODULES; do
  found=0
  for dir in "${{AP[@]}}"; do
    if [ -f "$dir/$mod/__manifest__.py" ] || [ -f "$dir/$mod/__openerp__.py" ]; then
      found=1; break
    fi
  done
  if [ "$found" -eq 0 ]; then
    MISSING="$MISSING $mod"
  fi
done
if [ -n "$MISSING" ]; then
  fail "installed module code missing on addons_path (Odoo will not boot):$MISSING"
fi
ok "all $(echo $INSTALLED_MODULES | wc -w) installed modules resolved on addons_path."

echo "== preflight passed =="
"""


# ---------------------------------------------------------------------------
# install.sh
# ---------------------------------------------------------------------------


def _render_install(report: ProvenanceReport, cfg: ReplicaConfig) -> str:
    # psql/pg_restore connection flags. Empty host => local socket/peer auth.
    conn_flags = ""
    if cfg.db_host:
        conn_flags += f" -h {_sh_quote(cfg.db_host)}"
    conn_flags += f" -p {cfg.db_port} -U {_sh_quote(cfg.db_user)}"

    neutralize_sql = "\n".join(
        [
            "UPDATE ir_config_parameter SET value = '0' WHERE key = 'mail.force.smtp.from' AND value IS NOT NULL;",
            "UPDATE ir_config_parameter SET value = 'smtp' WHERE key = 'mail.default.server';",
            "UPDATE ir_mail_server SET active = false, smtp_host = NULL, smtp_user = NULL, smtp_pass = NULL;",
            "UPDATE fetchmail_server SET active = false, password = NULL, \"user\" = NULL;",
            "UPDATE payment_provider SET state = 'disabled';",
        ]
    )

    pgpass_export = ""
    if cfg.db_password:
        pgpass_export = f'export PGPASSWORD={_sh_quote(cfg.db_password)}\n'

    return f"""#!/usr/bin/env bash
# install.sh -- restore the masked bundle and bring up the Odoo replica.
# Generated by odoo-synth. Run as root (or a sudoer) on the target box, from
# inside the kit directory. Idempotent-ish: it DROPs and recreates the DB.
set -euo pipefail

HERE="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
cd "$HERE"

DB_NAME={_sh_quote(cfg.db_name)}
DATA_DIR={_sh_quote(cfg.data_dir)}
HTTP_PORT={cfg.http_port}
SERVICE_NAME={_sh_quote(cfg.service_name)}
SERVICE_USER={_sh_quote(cfg.service_user)}
ADMIN_PASSWORD={_sh_quote(cfg.admin_password)}
PYTHON_BIN={_sh_quote(cfg.python_bin)}
{pgpass_export}
say() {{ echo "[install] $*"; }}
die() {{ echo "[install] ERROR: $*" >&2; exit 1; }}

PSQL="psql -X -v ON_ERROR_STOP=1{conn_flags}"
PSQL_DB="$PSQL -d $DB_NAME"

# --- 0. preflight ---
say "running preflight ..."
./preflight.sh || die "preflight failed; aborting."

# --- 1. (re)create the target database ---
say "recreating database $DB_NAME ..."
$PSQL -d postgres -c "DROP DATABASE IF EXISTS \\"$DB_NAME\\";"
$PSQL -d postgres -c "CREATE DATABASE \\"$DB_NAME\\" ENCODING 'UTF8' TEMPLATE template0;"
# Pin search_path to public. The masked dump may carry an 'odoo_synth' helper
# schema; without this, objects there could shadow public and break Odoo.
$PSQL -d postgres -c "ALTER DATABASE \\"$DB_NAME\\" SET search_path TO public;"

# --- 2. restore the masked dump ---
say "restoring db.dump (custom format) ..."
# pg_restore returns non-zero on --clean of a fresh DB (DROP of absent objects);
# that is benign, so we tolerate it and verify the outcome explicitly below.
set +e
pg_restore --no-owner --no-privileges --clean --if-exists \\
  {conn_flags.strip()} -d "$DB_NAME" "$HERE/db.dump"
set -e
# Verify a core Odoo table actually landed.
GOT="$($PSQL_DB -A -t -c "SELECT to_regclass('public.res_partner');" || true)"
[ "$GOT" = "res_partner" ] || die "restore verification failed: public.res_partner not present."
say "restore verified (public.res_partner present)."

# --- 3. filestore ---
if [ -d "$HERE/filestore" ] && [ -n "$(ls -A "$HERE/filestore" 2>/dev/null)" ]; then
  DEST="$DATA_DIR/filestore/$DB_NAME"
  say "copying filestore -> $DEST ..."
  mkdir -p "$DEST"
  cp -a "$HERE/filestore/." "$DEST/"
  chown -R "$SERVICE_USER":"$SERVICE_USER" "$DATA_DIR/filestore" 2>/dev/null || true
else
  say "no filestore in bundle (attachments dropped by masking policy); skipping."
fi

# --- 4. odoo.conf ---
say "installing /etc/odoo/odoo.conf ..."
mkdir -p /etc/odoo
cp "$HERE/odoo.conf" /etc/odoo/odoo.conf
mkdir -p "$DATA_DIR"
chown -R "$SERVICE_USER":"$SERVICE_USER" "$DATA_DIR" 2>/dev/null || true

# --- 5. neutralize (mail / fetchmail / payment providers) ---
say "applying neutralize SQL ..."
$PSQL_DB <<'NEUTRALIZE_SQL'
{neutralize_sql}
NEUTRALIZE_SQL

# --- 6. set the admin login password (pbkdf2-sha512, Odoo's passlib scheme) ---
say "setting admin password ..."
HASH="$("$PYTHON_BIN" - "$ADMIN_PASSWORD" <<'PYHASH'
import sys
try:
    from passlib.context import CryptContext
except Exception as exc:  # noqa: BLE001
    sys.stderr.write("passlib not available in target python: %s\\n" % exc)
    sys.exit(3)
ctx = CryptContext(schemes=["pbkdf2_sha512"])
sys.stdout.write(ctx.hash(sys.argv[1]))
PYHASH
)"
[ -n "$HASH" ] || die "failed to compute admin password hash."
# Resolve admin via xmlid base.user_admin, fallback to uid 2.
ADMIN_UID="$($PSQL_DB -A -t -c "SELECT res_id FROM ir_model_data WHERE module='base' AND name='user_admin' LIMIT 1;" || true)"
[ -n "$ADMIN_UID" ] || ADMIN_UID=2
$PSQL_DB -c "UPDATE res_users SET password='$HASH' WHERE id=$ADMIN_UID;"
say "admin password set for uid $ADMIN_UID."

# --- 7. systemd unit ---
say "installing systemd unit $SERVICE_NAME.service ..."
cp "$HERE/$SERVICE_NAME.service" "/etc/systemd/system/$SERVICE_NAME.service"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

# --- 8. HTTP health-check ---
say "waiting for Odoo to answer on :$HTTP_PORT ..."
for i in $(seq 1 60); do
  code="$(curl -s -o /dev/null -w '%{{http_code}}' "http://127.0.0.1:$HTTP_PORT/web/health" 2>/dev/null || true)"
  if [ "$code" = "200" ]; then
    say "health check OK (HTTP 200)."
    echo "== replica is up: http://<this-host>:$HTTP_PORT =="
    exit 0
  fi
  sleep 2
done
die "Odoo did not become healthy on :$HTTP_PORT within timeout (check: journalctl -u $SERVICE_NAME)."
"""


# ---------------------------------------------------------------------------
# odoo.conf
# ---------------------------------------------------------------------------


def _render_odoo_conf(report: ProvenanceReport, cfg: ReplicaConfig) -> str:
    master = cfg.master_password or "CHANGE_ME_master_password"
    db_host = cfg.db_host or "False"
    db_pass = cfg.db_password or "False"
    return f"""[options]
; odoo.conf for the masked replica -- generated by odoo-synth.
; Source: Odoo {report.odoo_series} (base {report.odoo_base_version}),
;         PostgreSQL major {report.postgres_major}.
admin_passwd = {master}
addons_path = {cfg.addons_path}
data_dir = {cfg.data_dir}
db_host = {db_host}
db_port = {cfg.db_port}
db_user = {cfg.db_user}
db_password = {db_pass}
db_name = {cfg.db_name}
dbfilter = ^{cfg.db_name}$
http_port = {cfg.http_port}
; Hardening for a replica: no DB management UI, run behind a proxy.
list_db = False
proxy_mode = True
"""


# ---------------------------------------------------------------------------
# systemd unit
# ---------------------------------------------------------------------------


def _render_systemd(cfg: ReplicaConfig) -> str:
    return f"""[Unit]
Description=Odoo (odoo-synth masked replica: {cfg.service_name})
After=network.target postgresql.service
Wants=postgresql.service

[Service]
Type=simple
User={cfg.service_user}
Group={cfg.service_user}
ExecStart={cfg.python_bin} {cfg.odoo_bin} -c /etc/odoo/odoo.conf
Restart=on-failure
RestartSec=5
KillMode=mixed

[Install]
WantedBy=multi-user.target
"""


# ---------------------------------------------------------------------------
# README
# ---------------------------------------------------------------------------


def _render_readme(report: ProvenanceReport, cfg: ReplicaConfig) -> str:
    policy = "warn (allowed)" if cfg.allow_mismatch else "hard fail"
    return f"""# odoo-synth replica kit

This directory is a **portable kit** that restores a PII-masked copy of a
production Odoo database and brings up a running replica on a fresh server.
It was generated by `odoo-synth replica` from a masked bundle. No part of the
source is contacted at install time.

## Source provenance (what this replica reproduces)

| Layer | Source value |
| --- | --- |
| Odoo series | `{report.odoo_series}` (base `{report.odoo_base_version}`) |
| PostgreSQL major | `{report.postgres_major}` |
| Installed modules | {report.installed_module_count} |
| DB encoding | `{report.db_encoding or "UTF8"}` |

Version-skew policy for PG-major / Odoo-series: **{policy}**.
Missing addon code on the target is **always** a hard failure.

## Contents

| File | Purpose |
| --- | --- |
| `preflight.sh` | Verify the target (PG major, Odoo series, Python, addon code). |
| `install.sh` | Restore `db.dump`, neutralize, set admin password, start systemd. |
| `odoo.conf` | Rendered Odoo config for this target. |
| `{cfg.service_name}.service` | systemd unit for the replica. |
| `provenance.json` | The detected source provenance (verbatim). |
| `db.dump` | *(from the bundle)* the masked PostgreSQL custom-format dump. |
| `filestore/` | *(from the bundle, may be empty)* attachment filestore. |

> Place this kit next to the bundle's `db.dump` and `filestore/`, or copy them
> into this directory, before running `install.sh`.

## Prerequisites on the target

* PostgreSQL major `{report.postgres_major}` server reachable as user
  `{cfg.db_user}` (create role/permissions ahead of time).
* Odoo {report.odoo_series} checked out with **all installed modules' code**
  present on: `{cfg.addons_path}`
* Python (`{cfg.python_bin}`) with Odoo's dependencies **and `passlib`**
  installed (used to hash the admin password).
* A service account `{cfg.service_user}`, and `curl` for the health check.

## Usage

```bash
# 1. verify the box is compatible (safe, read-only)
./preflight.sh

# 2. restore + provision + start (run as root / sudo)
sudo ./install.sh
```

On success the replica answers on `http://<host>:{cfg.http_port}` and the
admin login password has been reset to the value baked into `install.sh`.

## Safety notes

* Mail, fetchmail and payment providers are **neutralized** during install so
  the replica cannot send mail or reach payment gateways.
* `list_db = False` and `proxy_mode = True` are set; put the replica behind a
  TLS-terminating reverse proxy.
* `admin_passwd` in `odoo.conf` (the DB-management master password) is a
  placeholder if you did not pass `--master-password`; change it.
"""
