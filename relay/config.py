"""Configuration, read from environment variables once (DESIGN.md section 7.6).

Each milestone adds the variables it first uses (decision D24). M1: the relay_db connection
string. M3: the worker's settings, all optional with the defaults below. M4: the target
connection string and the three target timeouts.
`ADMIN_DSN` and `TARGET_MIGRATOR_PASSWORD` are not part of Settings: only dbinit and the
tests use them, never the running API or worker.
`RELAY_HOOK` is not here either: relay/hooks.py reads it, and it is unset outside tests.
"""

import os
import re
import socket
from dataclasses import dataclass

# What the LLM is told about the target database. The fake LLM ignores it; Groq (Phase 2)
# will need it to write sensible SQL.
SCHEMA_SUMMARY = """\
customers(id bigint primary key, email text, full_name text, created_at timestamptz)
orders(id bigint primary key, customer_id bigint references customers, total_cents integer,
       status text, created_at timestamptz)"""

# Extra seconds the lease must have beyond the two target timeouts (decision D33). It covers
# what the timeouts do not: opening the target connection, the lease re-check, the marker
# read, the COMMIT, and the final write to Relay.
LEASE_SAFETY_MARGIN_SECONDS = 10

_TIMEOUT_PATTERN = re.compile(r"^([0-9]+)(ms|s|min)$")
_SECONDS_PER_UNIT = {"ms": 0.001, "s": 1.0, "min": 60.0}


@dataclass(frozen=True)
class Settings:
    relay_db_dsn: str
    # The target database, logged in as relay_migrator (never the admin role).
    target_db_dsn: str
    # A name for this worker in the audit log (actor "worker:<worker_id>").
    worker_id: str = "worker"
    # How long a claim lasts before others may reclaim the job. Longer than the slowest
    # expected job, because there is no lease renewal (decision D5).
    lease_seconds: int = 60
    # How long an idle worker waits before looking for work again.
    poll_seconds: float = 1.0
    # Claims allowed per phase before the job is marked FAILED (decisions D8, D30).
    max_attempts: int = 5
    llm_provider: str = "fake"
    # Limits inside the target transaction, written as <whole number><ms|s|min>.
    # lock_timeout: the longest one statement may wait for a lock (another session's table
    # lock, or another worker's uncommitted marker).
    lock_timeout: str = "5s"
    # statement_timeout: the longest one statement may run in total.
    statement_timeout: str = "15s"
    # The longest the transaction may sit open doing nothing between statements; after that
    # Postgres ends the session and rolls the transaction back (DESIGN.md section 1 #5).
    target_idle_timeout: str = "30s"


def timeout_seconds(text: str) -> float:
    """Read a timeout such as '5s', '500ms', or '2min' as a number of seconds.

    Postgres accepts more spellings than this. Relay accepts only these three so the lease
    check below can be sure it read the value the same way Postgres will.
    """
    match = _TIMEOUT_PATTERN.match(text)
    if match is None:
        raise ValueError(f"timeout {text!r} must be a whole number followed by ms, s, or min (for example '5s')")
    return int(match.group(1)) * _SECONDS_PER_UNIT[match.group(2)]


def check_lease_outlasts_target_work(settings: Settings) -> None:
    """Refuse settings under which a healthy worker's lease could expire mid-execution.

    The rule (decision D33): LEASE_SECONDS > lock_timeout + statement_timeout + margin.
    Why that sum: in the target transaction the marker insert can wait up to lock_timeout
    for another worker's marker, and the DDL can run up to statement_timeout. Everything
    else is covered by the margin.

    A timeout of zero is refused too: in Postgres, 0 means "no timeout", so the target
    transaction would have no upper bound at all.

    This narrows the late-apply gap (DESIGN.md section 1 #5); it does not close it. A worker
    frozen by the operating system can still overrun any lease.
    """
    lock = timeout_seconds(settings.lock_timeout)
    statement = timeout_seconds(settings.statement_timeout)
    for name, value in (("LOCK_TIMEOUT", lock), ("STATEMENT_TIMEOUT", statement)):
        if value <= 0:
            raise ValueError(f"{name} must be greater than zero; 0 disables the timeout in Postgres")
    needed = lock + statement + LEASE_SAFETY_MARGIN_SECONDS
    if not settings.lease_seconds > needed:
        raise ValueError(
            f"LEASE_SECONDS ({settings.lease_seconds}) must be greater than LOCK_TIMEOUT ({lock:g}s) "
            f"+ STATEMENT_TIMEOUT ({statement:g}s) + a {LEASE_SAFETY_MARGIN_SECONDS}s safety margin "
            f"= {needed:g}s, so that a lease outlasts the target transaction"
        )


def _require(env: dict, name: str) -> str:
    try:
        return env[name]
    except KeyError:
        raise RuntimeError(f"environment variable {name} is not set (see .env.example)") from None


def load_settings(env: dict | None = None) -> Settings:
    env = os.environ if env is None else env
    return Settings(
        relay_db_dsn=_require(env, "RELAY_DB_DSN"),
        target_db_dsn=_require(env, "TARGET_DB_DSN"),
        # Default: host name plus process id, unique per worker container and per process.
        worker_id=env.get("WORKER_ID") or f"{socket.gethostname()}-{os.getpid()}",
        lease_seconds=int(env.get("LEASE_SECONDS", 60)),
        poll_seconds=float(env.get("POLL_SECONDS", 1.0)),
        max_attempts=int(env.get("MAX_ATTEMPTS", 5)),
        llm_provider=env.get("LLM_PROVIDER", "fake"),
        lock_timeout=env.get("LOCK_TIMEOUT", "5s"),
        statement_timeout=env.get("STATEMENT_TIMEOUT", "15s"),
        target_idle_timeout=env.get("TARGET_IDLE_TIMEOUT", "30s"),
    )


def load_admin_dsn(env: dict | None = None) -> str:
    env = os.environ if env is None else env
    return _require(env, "ADMIN_DSN")


def load_migrator_password(env: dict | None = None) -> str:
    env = os.environ if env is None else env
    return _require(env, "TARGET_MIGRATOR_PASSWORD")
