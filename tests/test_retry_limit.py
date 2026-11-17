"""The retry limit (decision D8): generation cases (M3) and execution cases (M4).

At the execution limit the worker must decide between SUCCEEDED and FAILED, and an earlier
attempt may have applied the migration, or may be applying it right now. So the check is
the same marker INSERT the normal path uses, inside a target transaction that is then
rolled back (`probe_marker`, decision D39; review finding F1). An INSERT of the same
migration_id WAITS for another transaction's uncommitted marker; a plain SELECT would not
see it and would not wait.
"""

import psycopg
import pytest

from relay import hooks, worker
from relay.llm.fake import FakeLLM
from relay.sqlhash import sha256_hex
from tests.helpers import (
    Background,
    CountingLLM,
    FlakyLLM,
    columns_of,
    events_of,
    expire_lease,
    insert_marker,
    markers_of,
    raise_once,
    seed_migration,
    use_up_attempts,
    wait_until_a_migrator_session_is_blocked,
)

ADD_NICKNAME = "ALTER TABLE public.customers ADD COLUMN nickname text"


def test_generation_fails_after_max_attempts(conn, make_worker):
    """Guarantee: a job that never gets drafted stops at the limit: with MAX_ATTEMPTS = 2 it
    is tried twice, and the third claim marks it FAILED with 'retry limit reached' without
    calling the LLM again.

    Invariant: 3 (the FAILED write is made by the worker holding the claim, guarded by its
    token). Without a limit, a job that crashes its worker every time would be reclaimed
    forever. Leases are backdated between attempts; nothing sleeps.
    """
    migration_id = seed_migration(conn, status="PENDING", description="fake:transient")
    llm = CountingLLM(FakeLLM())
    w1 = make_worker("w1", llm=llm, max_attempts=2)

    outcomes = [w1.run_once()]             # attempt 1: temporary error, lease abandoned
    expire_lease(conn, migration_id)
    outcomes.append(w1.run_once())         # attempt 2: reclaimed, claimed, same error
    expire_lease(conn, migration_id)
    outcomes.append(w1.run_once())         # attempt 3 is over the limit: FAILED, no LLM call

    row = conn.execute("SELECT status, error, attempts, lease_token FROM migrations WHERE id = %s", (migration_id,)).fetchone()
    assert outcomes == [worker.ABANDONED, worker.ABANDONED, worker.FAILED]
    assert row == {"status": "FAILED", "error": "retry limit reached", "attempts": 3, "lease_token": None}
    assert len(llm.calls) == 2
    assert w1.run_once() == worker.IDLE    # FAILED is terminal: nothing picks it up again


def test_execution_at_limit_with_marker_succeeds(conn, target, make_worker):
    """Guarantee: a job over the execution retry limit whose marker (with the approved hash)
    is already on the target is marked SUCCEEDED, not FAILED.

    Invariant: 4, and decisions D8 and D39: at the limit the worker asks the target before
    deciding, so a migration that really was applied is never recorded as failed. Setup: with
    MAX_ATTEMPTS = 1, attempt 1 commits on the target and then "dies" before telling Relay
    (a hook raises at `execute.after_target_commit`); attempt 2 is over the limit.
    """
    migration_id = seed_migration(conn, status="APPROVED", sql=ADD_NICKNAME)
    hooks.register("execute.after_target_commit", raise_once("simulated crash after the target commit"))
    w1 = make_worker("w1", max_attempts=1)

    with pytest.raises(RuntimeError, match="simulated crash"):
        w1.run_once()
    expire_lease(conn, migration_id)
    outcome = w1.run_once()

    row = conn.execute("SELECT status, error, attempts, lease_token FROM migrations WHERE id = %s", (migration_id,)).fetchone()
    assert outcome == worker.SUCCEEDED
    assert row == {"status": "SUCCEEDED", "error": None, "attempts": 2, "lease_token": None}
    assert markers_of(target) == {migration_id: sha256_hex(ADD_NICKNAME)}


def test_execution_at_limit_without_marker_fails(conn, target, make_worker):
    """Guarantee: a job over the execution retry limit with no marker on the target is marked
    FAILED with 'retry limit reached', and the worker does not attempt the DDL again.

    The check itself leaves nothing behind: it inserts a marker to see whether one exists,
    then rolls that insert back, so the target ends with no marker and no column.

    Invariant: 3 (the FAILED write is made by the worker holding the claim) and 4 (a marker
    exists on the target only together with its DDL). Setup: with MAX_ATTEMPTS = 1, attempt 1
    "dies" right after its claim, before any target work (a hook raises at
    `claim.after_commit`); attempt 2 is over the limit.
    """
    migration_id = seed_migration(conn, status="APPROVED", sql=ADD_NICKNAME)
    hooks.register("claim.after_commit", raise_once("simulated crash after the claim"))
    w1 = make_worker("w1", max_attempts=1)

    with pytest.raises(RuntimeError, match="simulated crash"):
        w1.run_once()
    expire_lease(conn, migration_id)
    outcome = w1.run_once()

    row = conn.execute("SELECT status, error, attempts, lease_token FROM migrations WHERE id = %s", (migration_id,)).fetchone()
    assert outcome == worker.FAILED
    assert row == {"status": "FAILED", "error": "retry limit reached", "attempts": 2, "lease_token": None}
    assert markers_of(target) == {}
    assert "nickname" not in columns_of(target, "customers")


def stale_attempt_in_flight(migrator, migration_id) -> None:
    """Inside the caller's open transaction on `migrator`: do what an earlier attempt does on
    the target (marker first, then the DDL) and stop before COMMIT. This stands in for a
    worker that stalled inside its target transaction after its lease ran out."""
    insert_marker(migrator, migration_id, sha256_hex(ADD_NICKNAME))
    migrator.execute(ADD_NICKNAME)


def relay_row(conn, migration_id) -> dict:
    return conn.execute(
        "SELECT status, error, lease_token FROM migrations WHERE id = %s", (migration_id,)
    ).fetchone()


def test_execution_at_limit_does_not_fail_a_job_whose_earlier_attempt_is_still_in_flight(
    conn, target, migrator, make_worker
):
    """Guarantee: a job over the execution retry limit is NOT marked FAILED while an earlier
    attempt is still inside its target transaction. The check waits for that attempt; when
    the wait outlasts lock_timeout the worker writes nothing (outcome `abandoned`, job still
    EXECUTING, no error). When the earlier attempt then commits, the next pass finds its
    marker and records SUCCEEDED.

    Invariant: 4 (the marker INSERT is the judge of "applied or not" on every path), and
    DESIGN.md section 1 #8 (never FAILED on an unknown outcome). This is review finding F1:
    with a plain SELECT of the marker, the worker saw "no marker" (uncommitted rows are
    invisible to other transactions), wrote FAILED, and the earlier attempt committed
    afterwards: Relay said FAILED while the target had the column.
    How it is forced: the earlier attempt is a transaction the test keeps open as
    relay_migrator (marker and DDL done, no COMMIT); the worker's lock_timeout is 300 ms.
    """
    migration_id = seed_migration(conn, status="APPROVED", sql=ADD_NICKNAME)
    use_up_attempts(conn, migration_id, 1)
    w1 = make_worker("w1", max_attempts=1, lock_timeout="300ms")

    with migrator.transaction():
        stale_attempt_in_flight(migrator, migration_id)
        outcome = Background(w1.run_once).join()          # claim number 2: over the limit
        assert outcome == worker.ABANDONED
        row = relay_row(conn, migration_id)
        assert (row["status"], row["error"], row["lease_token"] is not None) == ("EXECUTING", None, True)
    # The earlier attempt has now committed: the migration IS applied.

    expire_lease(conn, migration_id)
    assert w1.run_once() == worker.SUCCEEDED
    assert relay_row(conn, migration_id)["status"] == "SUCCEEDED"
    assert markers_of(target) == {migration_id: sha256_hex(ADD_NICKNAME)}
    assert "nickname" in columns_of(target, "customers")


def test_execution_at_limit_waits_for_an_in_flight_attempt_and_succeeds_when_it_commits(
    conn, target, migrator, make_worker
):
    """Guarantee: at the retry limit, the worker's check WAITS behind an earlier attempt's
    uncommitted marker; when that attempt commits, the same pass sees the committed marker
    with the approved hash and marks the job SUCCEEDED. No FAILED is ever written.

    Invariant: 4. The wait is the whole point: the verdict is reached only after the
    earlier attempt's outcome is known.
    How it is forced: the earlier attempt is an open transaction held by the test; the
    worker's target session is OBSERVED waiting on a lock before the test commits. No sleeps.
    """
    migration_id = seed_migration(conn, status="APPROVED", sql=ADD_NICKNAME)
    use_up_attempts(conn, migration_id, 1)
    w1 = make_worker("w1", max_attempts=1)

    with migrator.transaction():
        stale_attempt_in_flight(migrator, migration_id)
        thread = Background(w1.run_once)
        wait_until_a_migrator_session_is_blocked(target)   # the check is really waiting
        assert relay_row(conn, migration_id)["status"] == "EXECUTING"   # and no verdict yet
    # Leaving the block committed the earlier attempt, which releases the waiting check.

    assert thread.join() == worker.SUCCEEDED
    assert relay_row(conn, migration_id) == {"status": "SUCCEEDED", "error": None, "lease_token": None}
    assert markers_of(target) == {migration_id: sha256_hex(ADD_NICKNAME)}
    assert events_of(conn, migration_id) == [
        ("APPROVED", "EXECUTING", "worker:w1"),
        ("EXECUTING", "SUCCEEDED", "worker:w1"),
    ]


def test_execution_at_limit_waits_for_an_in_flight_attempt_and_fails_when_it_rolls_back(
    conn, target, migrator, make_worker
):
    """Guarantee: the same wait, but the earlier attempt rolls back. Now "not applied" is
    known to be true, so the job is marked FAILED with 'retry limit reached', and the target
    holds no marker and no column: neither the earlier attempt's nor the check's own.

    Invariant: 4 (the check's marker insert is rolled back, so no marker exists without its
    DDL) and 3 (the FAILED write carries the claim's token).
    How it is forced: as above; the test leaves its transaction with `psycopg.Rollback`
    after seeing the worker's session blocked.
    """
    migration_id = seed_migration(conn, status="APPROVED", sql=ADD_NICKNAME)
    use_up_attempts(conn, migration_id, 1)
    w1 = make_worker("w1", max_attempts=1)

    with migrator.transaction():
        stale_attempt_in_flight(migrator, migration_id)
        thread = Background(w1.run_once)
        wait_until_a_migrator_session_is_blocked(target)
        raise psycopg.Rollback      # the earlier attempt gives up

    assert thread.join() == worker.FAILED
    assert relay_row(conn, migration_id) == {"status": "FAILED", "error": "retry limit reached", "lease_token": None}
    assert markers_of(target) == {}
    assert "nickname" not in columns_of(target, "customers")


def test_execution_at_limit_with_a_different_hash_marker_fails_and_says_so(conn, target, make_worker):
    """Guarantee: a job over the execution retry limit whose marker on the target carries a
    DIFFERENT hash is marked FAILED, and the error names both facts: 'retry limit reached;
    target marker exists with a different hash'. The DDL is not run.

    Invariant: 4 (an existing marker counts as success only if its sql_hash matches the
    approved hash). A marker for this migration with other SQL is an anomaly someone should
    look at, so the message does not hide it behind the retry limit.
    """
    migration_id = seed_migration(conn, status="APPROVED", sql=ADD_NICKNAME)
    use_up_attempts(conn, migration_id, 1)
    other_hash = sha256_hex("ALTER TABLE public.customers ADD COLUMN something_else text")
    insert_marker(target, migration_id, other_hash)
    w1 = make_worker("w1", max_attempts=1)

    outcome = w1.run_once()

    assert outcome == worker.FAILED
    assert relay_row(conn, migration_id) == {
        "status": "FAILED",
        "error": "retry limit reached; target marker exists with a different hash",
        "lease_token": None,
    }
    assert markers_of(target) == {migration_id: other_hash}
    assert "nickname" not in columns_of(target, "customers")


def test_successful_draft_resets_attempts_to_zero(conn, make_worker):
    """Guarantee: the retry budget counts per phase, not per job: a draft that succeeded on
    its second try leaves `attempts = 0`, so execution (M4) starts with a full budget.

    Invariant: none of the six; decision D30. The full history stays in migration_events.
    """
    migration_id = seed_migration(conn, status="PENDING", description="add column nickname text to customers")
    w1 = make_worker("w1", llm=FlakyLLM(FakeLLM(), failures=1))

    w1.run_once()
    during = conn.execute("SELECT attempts FROM migrations WHERE id = %s", (migration_id,)).fetchone()["attempts"]
    expire_lease(conn, migration_id)
    w1.run_once()

    after = conn.execute("SELECT status, attempts FROM migrations WHERE id = %s", (migration_id,)).fetchone()
    assert during == 1
    assert after == {"status": "WAITING_FOR_APPROVAL", "attempts": 0}
