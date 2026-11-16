"""Shared test helpers. Grows with the milestones (DESIGN.md section 2)."""

import threading
import time
import uuid

from relay import db
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


def seed_migration(conn, status: str = "PENDING", description: str = "seeded row") -> uuid.UUID:
    """Insert a row directly in any status and return its id.

    This is test setup, not application behavior: it bypasses the API and writes no audit
    event. Tests may INSERT a row in any state this way but never UPDATE status
    (DESIGN.md section 7.2).
    """
    has_proposal = status in _NEEDS_PROPOSAL
    has_approval = status in _NEEDS_APPROVAL
    has_lease = status in _NEEDS_LEASE
    sql_hash = sha256_hex(SEED_SQL)
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
            "sql": SEED_SQL if has_proposal else None,
            "proposed_hash": sql_hash if has_proposal else None,
            "approved_hash": sql_hash if has_approval else None,
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
