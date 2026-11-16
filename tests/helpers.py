"""Shared test helpers. Grows with the milestones (DESIGN.md section 2)."""

import os
import subprocess
import sys
import threading
import time
import uuid

from relay import db
from relay.llm import LLMTransientError, Proposal
from relay.sqlhash import sha256_hex

# What each status requires to exist in the row, mirroring the CHECK constraints in
# db/relay_schema.sql. seed_migration fills these in so a seeded row is a legal row.
_NEEDS_PROPOSAL = {"WAITING_FOR_APPROVAL", "APPROVED", "EXECUTING", "SUCCEEDED", "REJECTED"}
_NEEDS_APPROVAL = {"APPROVED", "EXECUTING", "SUCCEEDED"}
_NEEDS_LEASE = {"DRAFTING", "EXECUTING"}

SEED_SQL = "ALTER TABLE public.customers ADD COLUMN seeded text"
# The hash a reviewer "saw" for a seeded proposal. Approve and reject must send exactly this.
SEED_HASH = sha256_hex(SEED_SQL)

# Upper bound for every Barrier.wait, Event.wait, and Thread.join in the tests. Nothing waits
# this long when the code is correct; it only stops a broken test from hanging forever
# (DESIGN.md section 7.3).
THREAD_TIMEOUT = 10


def seed_migration(
    conn,
    status: str = "PENDING",
    description: str = "seeded row",
    *,
    sql: str = SEED_SQL,
    approved_hash: str | None = None,
) -> uuid.UUID:
    """Insert a row directly in any status and return its id.

    This is test setup, not application behavior: it bypasses the API and writes no audit
    event. Tests may INSERT a row in any state this way but never UPDATE status
    (DESIGN.md section 7.2).

    `sql` is the stored proposal (M4 tests give each job its own column). `approved_hash`
    defaults to the hash of `sql`; pass another value to seed a row whose stored SQL no
    longer matches what was approved.
    """
    has_proposal = status in _NEEDS_PROPOSAL
    has_approval = status in _NEEDS_APPROVAL
    has_lease = status in _NEEDS_LEASE
    sql_hash = sha256_hex(sql)
    row = conn.execute(
        """
        INSERT INTO migrations (description, status, proposed_sql, proposed_sql_hash,
                                approved_sql_hash, lease_token, lease_expires_at)
        VALUES (%(description)s, %(status)s, %(sql)s, %(proposed_hash)s, %(approved_hash)s,
                %(token)s, %(expires)s)
        RETURNING id
        """,
        {
            "description": description,
            "status": status,
            "sql": sql if has_proposal else None,
            "proposed_hash": sql_hash if has_proposal else None,
            "approved_hash": (approved_hash or sql_hash) if has_approval else None,
            "token": uuid.uuid4() if has_lease else None,
            # A far-future timestamp: nothing in M1 looks at it, and M3 tests backdate it.
            "expires": "2999-01-01T00:00:00Z" if has_lease else None,
        },
    ).fetchone()
    return row["id"]


class Background:
    """Run `fn()` in a thread and keep its result or its exception.

    A plain thread swallows exceptions: the test would pass while the thread had crashed.
    `join()` re-raises whatever the thread raised, and fails if the thread is still running
    after THREAD_TIMEOUT.
    """

    def __init__(self, fn):
        self.result = None
        self.error = None
        self._fn = fn
        # daemon=True: a thread stuck on a lock cannot keep the test process alive.
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        try:
            self.result = self._fn()
        except BaseException as error:  # noqa: BLE001 - reported to the test by join()
            self.error = error

    def join(self):
        self._thread.join(THREAD_TIMEOUT)
        assert not self._thread.is_alive(), "thread did not finish in time"
        if self.error is not None:
            raise self.error
        return self.result


def run_behind_barrier(dsn: str, actions: list) -> list:
    """Run every `action(conn)` at the same moment, each in its own thread and connection.

    A Barrier is a gate that opens only when all N threads are standing at it. Every thread
    connects first and then waits at the gate, so the slow part (connecting) is over before
    the race starts and all N actions hit Postgres together. No sleeps (DESIGN.md section 7.3).

    Returns the results in the same order as `actions`.
    """
    barrier = threading.Barrier(len(actions))

    def runner(action):
        def run():
            with db.connect(dsn) as conn:
                barrier.wait(THREAD_TIMEOUT)
                return action(conn)
        return run

    threads = [Background(runner(action)) for action in actions]
    return [thread.join() for thread in threads]


def wait_until_blocked(conn, pid: int) -> None:
    """Return once the Postgres session `pid` is waiting for a lock; fail after THREAD_TIMEOUT.

    This proves the other session is really blocked, not just slow. `pg_stat_activity` is
    Postgres's live list of sessions; `wait_event_type = 'Lock'` means "queued behind another
    transaction's lock". The short pause between looks only observes the race: the race itself
    is created by the test holding a transaction open, never by timing.
    """
    deadline = time.monotonic() + THREAD_TIMEOUT
    while time.monotonic() < deadline:
        row = conn.execute(
            "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s", (pid,)
        ).fetchone()
        if row is not None and row["wait_event_type"] == "Lock":
            return
        time.sleep(0.01)
    raise AssertionError(f"session {pid} never blocked on a lock")


def expire_lease(conn, migration_id) -> None:
    """Make a lease look expired by moving its expiry one second into the past.

    Tests never wait for a real lease to run out (DESIGN.md section 7.4). This touches only
    `lease_expires_at`, never `status`: the status still changes only through the helper.
    """
    conn.execute(
        "UPDATE migrations SET lease_expires_at = now() - interval '1 second' WHERE id = %s",
        (migration_id,),
    )


class FixedLLM:
    """A test LLM that always proposes the same SQL. Lets a test tell two workers' results apart."""

    def __init__(self, sql: str, risk_level: str = "LOW"):
        self._proposal = Proposal(sql=sql, risk_level=risk_level, risk_reason="fixed test proposal")

    def propose(self, description: str, schema_summary: str) -> Proposal:
        return self._proposal


class CountingLLM:
    """Wraps another LLM and records every description it was asked about (thread-safe)."""

    def __init__(self, inner):
        self._inner = inner
        self._lock = threading.Lock()
        self.calls: list[str] = []

    def propose(self, description: str, schema_summary: str) -> Proposal:
        with self._lock:
            self.calls.append(description)
        return self._inner.propose(description, schema_summary)


class FlakyLLM:
    """Raises LLMTransientError for the first `failures` calls, then behaves like `inner`."""

    def __init__(self, inner, failures: int = 1):
        self._inner = inner
        self._failures_left = failures
        self.calls = 0

    def propose(self, description: str, schema_summary: str) -> Proposal:
        self.calls += 1
        if self._failures_left > 0:
            self._failures_left -= 1
            raise LLMTransientError("flaky test LLM: try again later")
        return self._inner.propose(description, schema_summary)


def columns_of(target, table: str) -> set[str]:
    """The column names of a demo table on the target database, as it is right now."""
    rows = target.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = %s",
        (table,),
    ).fetchall()
    return {row["column_name"] for row in rows}


def markers_of(target) -> dict:
    """Every marker on the target: {migration_id: sql_hash}. The primary key on migration_id
    means there can never be two markers for one migration."""
    rows = target.execute("SELECT migration_id, sql_hash FROM relay_applied").fetchall()
    return {row["migration_id"]: row["sql_hash"] for row in rows}


def insert_marker(target, migration_id, sql_hash: str) -> None:
    """Test setup: put a marker on the target as if an earlier attempt had committed there."""
    target.execute(
        "INSERT INTO relay_applied (migration_id, sql_hash) VALUES (%s, %s)", (migration_id, sql_hash)
    )


def events_of(conn, migration_id) -> list[tuple]:
    """The audit trail of one migration, oldest first, as (from, to, actor) tuples."""
    rows = conn.execute(
        "SELECT from_status, to_status, actor FROM migration_events WHERE migration_id = %s ORDER BY id",
        (migration_id,),
    ).fetchall()
    return [(row["from_status"], row["to_status"], row["actor"]) for row in rows]


def raise_once(message: str):
    """A hook function that raises RuntimeError the first time it runs and does nothing after.

    Stands in for "the worker died at this line" in single-process tests: the exception leaves
    `run_once` without any further write, exactly what a dead worker would have written.
    (Real process deaths are tested with `spawn_worker`.)
    """
    already_raised = []

    def hook():
        if already_raised:
            return
        already_raised.append(True)
        raise RuntimeError(message)

    return hook


def read_until(process: subprocess.Popen, text: str) -> str:
    """Read a worker process's output until a line contains `text`; return everything read.

    Runs in a Background thread so a worker that never prints the line fails the test after
    THREAD_TIMEOUT instead of hanging it.
    """
    def read():
        seen = []
        for line in process.stdout:
            seen.append(line)
            if text in line:
                return "".join(seen)
        raise AssertionError(f"worker exited without printing {text!r}; output:\n{''.join(seen)}")

    return Background(read).join()


def spawn_worker(settings, worker_id: str, hook: str) -> subprocess.Popen:
    """Start a real worker process (`python -m relay.worker`) against the test database.

    `hook` is a RELAY_HOOK value such as "claim.after_commit:crash", which makes the process
    die on the spot at that named point, with no cleanup, like `kill -9` (DESIGN.md section 7.5).
    """
    env = {
        **os.environ,
        "RELAY_DB_DSN": settings.relay_db_dsn,
        "TARGET_DB_DSN": settings.target_db_dsn,
        "WORKER_ID": worker_id,
        "RELAY_HOOK": hook,
    }
    return subprocess.Popen(
        [sys.executable, "-m", "relay.worker"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
