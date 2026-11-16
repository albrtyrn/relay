"""Configuration, read from environment variables once (DESIGN.md section 7.6).

Each milestone adds the variables it first uses (decision D24). M1: the relay_db connection
string. M3: the worker's settings, all optional with the defaults below.
`ADMIN_DSN` is not part of Settings: only dbinit and the tests use it, never the running API.
`RELAY_HOOK` is not here either: relay/hooks.py reads it, and it is unset outside tests.
"""

import os
import socket
from dataclasses import dataclass

# What the LLM is told about the target database. The fake LLM ignores it; Groq (Phase 2)
# will need it to write sensible SQL.
SCHEMA_SUMMARY = """\
customers(id bigint primary key, email text, full_name text, created_at timestamptz)
orders(id bigint primary key, customer_id bigint references customers, total_cents integer,
       status text, created_at timestamptz)"""


@dataclass(frozen=True)
class Settings:
    relay_db_dsn: str
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


def _require(env: dict, name: str) -> str:
    try:
        return env[name]
    except KeyError:
        raise RuntimeError(f"environment variable {name} is not set (see .env.example)") from None


def load_settings(env: dict | None = None) -> Settings:
    env = os.environ if env is None else env
    return Settings(
        relay_db_dsn=_require(env, "RELAY_DB_DSN"),
        # Default: host name plus process id, unique per worker container and per process.
        worker_id=env.get("WORKER_ID") or f"{socket.gethostname()}-{os.getpid()}",
        lease_seconds=int(env.get("LEASE_SECONDS", 60)),
        poll_seconds=float(env.get("POLL_SECONDS", 1.0)),
        max_attempts=int(env.get("MAX_ATTEMPTS", 5)),
        llm_provider=env.get("LLM_PROVIDER", "fake"),
    )


def load_admin_dsn(env: dict | None = None) -> str:
    env = os.environ if env is None else env
    return _require(env, "ADMIN_DSN")
