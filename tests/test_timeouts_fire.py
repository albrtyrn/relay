"""The three target timeouts actually firing (DESIGN.md section 4.3; decision D9; added after
the review, REVIEW.md section 5).

`test_execute.py` shows the three settings are in force inside the target transaction.
These tests make each one go off and check what the worker does about it:

- lock_timeout: how long one statement may WAIT for a lock another session holds.
- statement_timeout: how long one statement may run in total, waiting included.
- idle_in_transaction_session_timeout: how long a session may sit inside an open
  transaction doing nothing before the server ends the session.

What the worker must do depends on WHERE the timeout hits (DESIGN.md section 1 #6):

    during the DDL statement  -> the migration FAILED; nothing is left on the target (D9)
    during the marker step    -> contention, not failure: abandon the lease, retry later
    session ended by the server -> outcome unknown: abandon the lease, never FAILED

About time in these tests: each one really waits about 300 ms for a real timeout. That is
the thing under test, not a sleep used to create a race. The blocking situation itself is
forced: the test holds a lock (or an uncommitted marker) in a transaction it keeps open.
The worker runs in a `Background` thread, so if a timeout ever stopped firing the test
would fail after THREAD_TIMEOUT instead of hanging behind its own lock.
"""

import psycopg

from relay import hooks, worker
from relay.sqlhash import sha256_hex
from tests.helpers import (
    Background,
    columns_of,
    events_of,
    expire_lease,
    insert_marker,
    markers_of,
    seed_migration,
    wait_until_migrator_sessions_are_gone,
)

ADD_NICKNAME = "ALTER TABLE public.customers ADD COLUMN nickname text"
SHORT = "300ms"


def row_of(conn, migration_id) -> dict:
    return conn.execute(
        "SELECT status, error, lease_token FROM migrations WHERE id = %s", (migration_id,)
    ).fetchone()


def hold_a_read_lock_on_customers(target) -> None:
    """Inside the caller's open transaction: take the lock an ordinary long-running SELECT
    holds on a table (ACCESS SHARE). ALTER TABLE needs the strongest lock (ACCESS EXCLUSIVE),
    which must wait for every other lock on the table, even a reader's."""
    target.execute("LOCK TABLE public.customers IN ACCESS SHARE MODE")


def test_lock_timeout_during_ddl_marks_failed_and_leaves_target_clean(conn, target, make_worker):
    """Guarantee: when the DDL waits for a table lock longer than lock_timeout, the job is
    marked FAILED with Postgres's lock-timeout error (SQLSTATE 55P03), on its first attempt,
    and the target keeps neither marker nor column.

    Invariant: 4 (the marker rolls back with the DDL, so a failed attempt leaves no marker
    that a later attempt could mistake for success), and decision D9: a DDL error, timeouts
    included, is terminal.
    How it is forced: another session holds a read lock on `customers` in an open
    transaction while the worker runs; the worker's lock_timeout is 300 ms.
    """
    migration_id = seed_migration(conn, status="APPROVED", sql=ADD_NICKNAME)
    w1 = make_worker("w1", lock_timeout=SHORT)

    with target.transaction():
        hold_a_read_lock_on_customers(target)
        outcome = Background(w1.run_once).join()

    assert outcome == worker.FAILED
    row = row_of(conn, migration_id)
    assert (row["status"], row["lease_token"]) == ("FAILED", None)
    assert row["error"].startswith("ddl: 55P03")
    assert markers_of(target) == {}
    assert "nickname" not in columns_of(target, "customers")
    assert events_of(conn, migration_id) == [
        ("APPROVED", "EXECUTING", "worker:w1"),
        ("EXECUTING", "FAILED", "worker:w1"),
    ]


def test_statement_timeout_during_ddl_marks_failed_and_leaves_target_clean(conn, target, make_worker):
    """Guarantee: when the DDL statement runs (here: waits) longer than statement_timeout,
    the job is marked FAILED with Postgres's statement-timeout error (SQLSTATE 57014) and
    the target keeps neither marker nor column.

    Invariant: 4 and decision D9, as in the test above, for the other timeout.
    How it is forced: the same held read lock, with statement_timeout at 300 ms and
    lock_timeout at 5 s, so statement_timeout is the one that fires. (If it did not fire,
    lock_timeout would, and the SQLSTATE assertion would fail.)
    """
    migration_id = seed_migration(conn, status="APPROVED", sql=ADD_NICKNAME)
    w1 = make_worker("w1", lock_timeout="5s", statement_timeout=SHORT)

    with target.transaction():
        hold_a_read_lock_on_customers(target)
        outcome = Background(w1.run_once).join()

    assert outcome == worker.FAILED
    row = row_of(conn, migration_id)
    assert (row["status"], row["lease_token"]) == ("FAILED", None)
    assert row["error"].startswith("ddl: 57014")
    assert markers_of(target) == {}
    assert "nickname" not in columns_of(target, "customers")


def test_lock_timeout_on_the_marker_step_abandons_and_the_retry_succeeds(conn, target, make_worker):
    """Guarantee: when the marker INSERT waits longer than lock_timeout behind another
    transaction's uncommitted marker for the same migration, the worker writes nothing to
    Relay (outcome `abandoned`; the job stays EXECUTING, no error). Once the other
    transaction has rolled back and the lease has expired, the retry applies the migration.

    Invariant: 4, and DESIGN.md section 1 #6: a timeout on the marker step is contention
    between two attempts, not a failed migration. Marking FAILED here would be wrong twice
    over: nothing was tried, and the other attempt may be about to commit.
    How it is forced: the test inserts the marker inside a transaction it keeps open, runs
    the worker (lock_timeout 300 ms) against it, then rolls its transaction back.
    """
    migration_id = seed_migration(conn, status="APPROVED", sql=ADD_NICKNAME)
    w1 = make_worker("w1", lock_timeout=SHORT)

    with target.transaction():
        insert_marker(target, migration_id, sha256_hex(ADD_NICKNAME))     # uncommitted
        outcome = Background(w1.run_once).join()
        raise psycopg.Rollback      # the other attempt gives up: leave the block by rolling back

    assert outcome == worker.ABANDONED
    row = row_of(conn, migration_id)
    assert (row["status"], row["error"], row["lease_token"] is not None) == ("EXECUTING", None, True)
    assert markers_of(target) == {}
    assert "nickname" not in columns_of(target, "customers")

    expire_lease(conn, migration_id)
    assert w1.run_once() == worker.SUCCEEDED
    assert markers_of(target) == {migration_id: sha256_hex(ADD_NICKNAME)}


def test_idle_in_transaction_timeout_ends_a_stalled_target_transaction(conn, target, make_worker):
    """Guarantee: when a worker stalls inside its target transaction for longer than the idle
    timeout, the SERVER ends the session and rolls the transaction back (no marker, no
    column). When the worker resumes, its next statement fails on the dead connection; it
    treats that as an unknown outcome (`abandoned`, job still EXECUTING, no error), not as a
    failed DDL. The retry after expiry succeeds.

    Invariant: 4 and DESIGN.md section 1 #8 (never FAILED on an unknown outcome). This is
    also one of the two things that narrow the late-apply gap (section 1 #5): a stalled
    transaction cannot stay open on the target indefinitely.
    The worker's failing statement IS the DDL, and the server's "session terminated" error
    carries a SQLSTATE, so only the "is the connection still healthy?" check
    (`_rolled_back_cleanly`) keeps this from being reported as a failed DDL.
    How it is forced: a hook at `execute.after_marker_insert` (inside the transaction, after
    the marker) holds the worker there until the server has ended its session; the worker's
    idle timeout is 300 ms.
    """
    migration_id = seed_migration(conn, status="APPROVED", sql=ADD_NICKNAME)
    w1 = make_worker("w1", target_idle_timeout=SHORT)
    hooks.register("execute.after_marker_insert", lambda: wait_until_migrator_sessions_are_gone(target))

    outcome = w1.run_once()

    assert outcome == worker.ABANDONED
    row = row_of(conn, migration_id)
    assert (row["status"], row["error"], row["lease_token"] is not None) == ("EXECUTING", None, True)
    assert markers_of(target) == {}
    assert "nickname" not in columns_of(target, "customers")

    hooks.clear()
    expire_lease(conn, migration_id)
    assert w1.run_once() == worker.SUCCEEDED
    assert markers_of(target) == {migration_id: sha256_hex(ADD_NICKNAME)}
