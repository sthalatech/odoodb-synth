"""Shared pg_dump/pg_restore invocation with automatic version-mismatch
fallback to the scratch stack's own container.

Why this exists: docker/docker-compose.scratch.yml pins postgres-anon to a
specific PostgreSQL major (see the image tag there) so the `anon` extension
version is reproducible. That major will often be *newer* than the major a
developer's host ships (and newer, in general, than the source Odoo
instance's Postgres major -- Odoo installs commonly run whatever major was
current when they were provisioned, which lags the latest Postgres release).
libpq's wire protocol guarantees pg_dump/pg_restore can be used from an
OLDER client against a NEWER server in many cases, but pg_dump specifically
refuses outright when the server major is newer than the client major
("aborting because of server version mismatch") -- there is no client-side
workaround for that other than running a pg_dump binary from the server's
own major.

Before this module existed, the ONLY way to get `odoo-synth snapshot` /
`odoo-synth up` working against the bundled scratch stack on a host with an
older pg_dump was to hand-set five separate environment variables
(ODOO_SYNTH_PG_DUMP, ODOO_SYNTH_PG_RESTORE, ODOO_SYNTH_DUMP_DB_URL,
ODOO_SYNTH_RESTORE_DB_URL, ODOO_SYNTH_PACKAGE_DB_URL) -- values that only
appeared, undocumented, in this project's own integration tests. The
documented README Quickstart does not mention any of this, so the tool
failed on first run for essentially anyone whose host pg_dump wasn't
already newer than or equal to whatever image tag docker-compose.scratch.yml
happens to pin. That's the gap this module closes: detect the failure and
retry automatically through the scratch container's own (matching) pg_dump/
pg_restore, with no environment variables required for the common case.

Explicit ODOO_SYNTH_PG_DUMP / ODOO_SYNTH_PG_RESTORE / *_DB_URL overrides (used
by the test suite, and available for any non-default container name/setup)
still take priority when set -- this module only *adds* an automatic path
for everyone else, it doesn't remove the escape hatch.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Default container name from docker/docker-compose.scratch.yml's
# `container_name: odoo-synth-postgres-anon`. Overridable in case a caller
# renamed the service (e.g. running multiple scratch stacks side by side).
DEFAULT_CONTAINER = "odoo-synth-postgres-anon"
CONTAINER_ENV = "ODOO_SYNTH_SCRATCH_CONTAINER"

# Two distinct failure modes show up depending on the tool and direction of
# the mismatch:
#   pg_dump against a NEWER server:  "aborting because of server version mismatch"
#   pg_restore reading a dump produced by a NEWER pg_dump: "unsupported
#     version (1.16) in file header" -- the dump's internal archive format
#     version is newer than this pg_restore build understands. Found running
#     the actual pipeline end-to-end against darkstore: package() dumps via
#     the (newer) scratch container's pg_dump, and the artifact then needs
#     to be pg_restore'd by a host binary that's never seen that archive
#     version.
_VERSION_MISMATCH_RE = re.compile(
    r"server version mismatch|unsupported version .* in file header",
    re.IGNORECASE,
)


class PgToolError(Exception):
    """Raised when a pg_dump/pg_restore invocation fails and no fallback
    (explicit override or auto-detected container) could make it succeed."""


def _scratch_container() -> str:
    return os.environ.get(CONTAINER_ENV, DEFAULT_CONTAINER)


def _container_running(name: str) -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        proc = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", name],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return False
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def _in_container_url(db_url: str) -> str:
    """Rewrite a host-facing DB URL to the container-internal form (as seen
    from inside the scratch container via `docker exec`): netloc becomes
    localhost:5432 (Postgres always listens there inside the container
    regardless of the host-side port mapping -- docker-compose.scratch.yml
    maps ${POSTGRES_PORT:-5433}:5432), and any query-string params that
    steer the connection to a HOST-side address are dropped.

    The query-string drop matters because libpq URI params (host=,
    hostaddr=, port=) take priority over the netloc authority component --
    a URL like ``postgresql://user@/db?host=/var/run/postgresql`` (the
    standard way to point at a non-default local Unix socket directory, and
    what a real Postgres install commonly needs) would otherwise keep
    routing the in-container pg_dump/pg_restore at the HOST's socket path,
    which doesn't exist inside the container -- silently defeating the
    netloc rewrite above. Found running the real provisioning pipeline
    end-to-end against darkstore.
    """
    parts = urlsplit(db_url)
    userinfo = ""
    if parts.username:
        userinfo = parts.username
        if parts.password:
            userinfo += f":{parts.password}"
        userinfo += "@"
    netloc = f"{userinfo}localhost:5432"
    # Strip host/hostaddr/port query params -- they'd override netloc above
    # with a HOST-side address that's meaningless inside the container.
    kept_query = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in ("host", "hostaddr", "port")
    ]
    query = urlencode(kept_query)
    return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))


def _is_version_mismatch(stderr: str) -> bool:
    return bool(_VERSION_MISMATCH_RE.search(stderr or ""))


def run_pg_tool(
    tool: str,
    args: list[str],
    db_url: str,
    *,
    cmd_env: str,
    url_envs: tuple[str, ...] = (),
    stdin: bytes | None = None,
    input_file=None,
    output_file=None,
    url_as_flag: str | None = None,
    dbname: str | None = None,
    tolerate_nonzero_exit: bool = False,
) -> subprocess.CompletedProcess:
    """Run `<tool> <args...> <db_url>`, honoring the explicit env overrides
    first, then automatically retrying through the scratch container's own
    pg_dump/pg_restore if the host binary reports a server-version mismatch.

    `args` should NOT include the db_url -- it's appended (as the final
    positional arg, or as `<url_as_flag> <url>` if url_as_flag is given --
    pg_restore takes its target as `-d <url>` rather than a trailing
    positional) after resolving which URL form to use, since the
    container-routed retry needs a different URL (container-internal
    host:port) than the direct host invocation.

    `cmd_env` is the ODOO_SYNTH_PG_DUMP/ODOO_SYNTH_PG_RESTORE-style override
    var name; `url_envs` are checked in order for an explicit in-container
    URL override (e.g. ODOO_SYNTH_DUMP_DB_URL) before falling back to
    `db_url` unchanged (explicit path) or the auto-rewritten form (auto
    path).

    `dbname`, if given, is substituted for a literal ``{dbname}`` placeholder
    in an explicit url_envs override -- provision.py restores into a
    dynamically-named target DB, so a single static env var (set once,
    outside this call) needs a way to reference that name. Without the
    placeholder the override is used as-is.

    `tolerate_nonzero_exit`: pg_restore --clean --if-exists routinely exits
    1 with "errors ignored on restore" for entirely benign reasons (e.g.
    DROP EXTENSION on an object something else still depends on, or DROP-
    IF-EXISTS notices on a fresh DB) -- callers that restore should set this
    True and verify success their own way afterward (checking a known table
    exists), matching the pre-existing behavior in cli.py/provision.py this
    module replaced. pg_dump callers should leave this False: a non-zero
    pg_dump exit is never benign.
    """
    explicit_cmd = os.environ.get(cmd_env)
    explicit_url = next((os.environ.get(v) for v in url_envs if os.environ.get(v)), None)
    if explicit_url and dbname is not None:
        explicit_url = explicit_url.replace("{dbname}", dbname)

    def _run(cmd: list[str], url: str) -> subprocess.CompletedProcess:
        url_part = [url_as_flag, url] if url_as_flag else [url]
        full = cmd + args + url_part
        if output_file is not None:
            # Binary stdout (e.g. pg_dump -Fc) -> write straight to the file
            # handle, capture only stderr as text for error reporting.
            output_file.seek(0)
            output_file.truncate()
            proc = subprocess.run(
                full, stdout=output_file, stderr=subprocess.PIPE, text=False,
            )
            return subprocess.CompletedProcess(
                proc.args, proc.returncode, stdout=None,
                stderr=proc.stderr.decode("utf-8", "replace") if proc.stderr else "",
            )
        kw: dict = {"capture_output": True, "text": True}
        if stdin is not None:
            kw["input"] = stdin
        elif input_file is not None:
            # Rewind so a retry (host attempt failed, falling back to the
            # container) re-reads from the start rather than EOF.
            input_file.seek(0)
            kw["stdin"] = input_file
        return subprocess.run(full, **kw)

    # 1. Explicit override (or plain default binary) -- the existing,
    #    documented escape hatch. Always tried first and, if it succeeds or
    #    fails for a reason OTHER than version mismatch, this is the final
    #    result (no silent double-running against the wrong DB).
    cmd = shlex.split(explicit_cmd) if explicit_cmd else [tool]
    url = explicit_url or db_url
    proc = _run(cmd, url)
    if proc.returncode == 0:
        return proc
    if not _is_version_mismatch(proc.stderr):
        if tolerate_nonzero_exit:
            return proc
        raise PgToolError(
            f"{tool} failed (exit {proc.returncode}): {proc.stderr}"
        )

    # 2. Version mismatch. If the user already pinned an explicit container
    #    override, don't second-guess it -- surface the failure as-is.
    if explicit_cmd or explicit_url:
        raise PgToolError(
            f"{tool} failed (exit {proc.returncode}) even with an explicit "
            f"override ({cmd_env}/{url_envs}): {proc.stderr}"
        )

    # 3. Auto-fallback: route through the scratch container's own matching
    #    pg_dump/pg_restore, translating the URL to the in-container form.
    container = _scratch_container()
    if not _container_running(container):
        raise PgToolError(
            f"{tool} failed due to a Postgres major-version mismatch, and no "
            f"running '{container}' container was found to retry through "
            f"(docker not installed, or the scratch stack isn't up -- see "
            f"`docker compose -f docker/docker-compose.scratch.yml up -d`). "
            f"Original error: {proc.stderr}"
        )
    container_cmd = ["docker", "exec", "-i", container, tool]
    container_url = _in_container_url(url)
    proc2 = _run(container_cmd, container_url)
    if proc2.returncode != 0 and not tolerate_nonzero_exit:
        raise PgToolError(
            f"{tool} failed on host (version mismatch) AND via the "
            f"'{container}' container fallback: {proc2.stderr}"
        )
    return proc2
