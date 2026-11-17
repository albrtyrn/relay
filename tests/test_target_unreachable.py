"""An unreachable target does not use up the retry limit (decision D41; review finding F4(b)).

`attempts` goes up by one at every claim (DESIGN.md section 4.2), and a job claimed more
than MAX_ATTEMPTS times in one phase is marked FAILED (decision D8). That limit exists to
stop a job that keeps failing. It should not punish a job for a target database that was
down: before this change, an outage longer than MAX_ATTEMPTS leases marked every approved
job FAILED, "retry limit reached", although its SQL had never been tried once.

The rule: if the worker cannot even OPEN its connection to the target, nothing was tried
there, so the pass gives its attempt back. It does that with one write through the
transition helper, EXECUTING -> EXECUTING (the status stays; `attempts` goes down by one),
guarded by its lease token and recorded as an audit event. The lease is kept, so the job
stays out of the queue until the lease expires (decision D7): that pause is the backoff.

What still counts as an attempt: everything after the connection opened (a connection lost
mid-transaction, a timeout on the marker step, a crash), because target work was started
or its outcome is unknown.

What this costs, on purpose: a target that never becomes reachable (say, a wrong password)
is retried once per lease forever and the job never fails. So each such pass logs at ERROR
with the driver's reason. (psycopg reports no SQLSTATE code for a failed connection, only
the text, for example "password authentication failed".)

How the outage is made: the worker gets a target connection string that points at a port
nothing listens on (refused at once), or one with a wrong password. Real Postgres, no mocks.
"""

import logging
import threading

import pytest
from psycopg.conninfo import make_conninfo

from relay import hooks, worker
from relay.claim import claim_one, reclaim_expired
from relay.sqlhash import sha256_hex
from tests.helpers import (
    THREAD_TIMEOUT,
    Background,
    columns_of,
    events_of,
    expire_lease,
    markers_of,
    seed_migration,
    use_up_attempts,
)

ADD_NICKNAME = "ALTER TABLE public.customers ADD COLUMN nickname text"
NOT_COUNTED = "target unreachable; attempt not counted"
REFUND_EVENT = ("EXECUTING", "EXECUTING", "worker:down")


@pytest.fixture
def unreachable_dsn(settings) -> str:
    """The worker's target connection string, aimed at a port that refuses connections."""
    return make_conninfo(settings.target_db_dsn, host="127.0.0.1", port=1)


def row_of(conn, migration_id) -> dict:
    return conn.execute(
        "SELECT status, error, attempts, lease_token FROM migrations WHERE id = %s", (migration_id,)
    ).fetchone()


def test_unreachable_target_gives_the_attempt_back_and_keeps_the_lease(conn, target, make_worker, unreachable_dsn):
    """Guarantee: when the target connection cannot be opened, the pass ends `abandoned`;
    the job is still EXECUTING under the same lease, with no error; `attempts` is back to
    what it was before the claim (0); and the audit log shows the refund as one
    EXECUTING -> EXECUTING event with the detail 'target unreachable; attempt not counted'.
    The target is untouched.

    Invariant: 1 and 3. The refund goes through the transition helper, checks the worker's
    lease token and the EXECUTING status, and commits together with its audit event.
    Keeping the lease (not releasing the job) is decision D7: the job waits out one lease
    before anyone retries it.
    """
    migration_id = seed_migration(conn, status="APPROVED", sql=ADD_NICKNAME)
    down = make_worker("down", target_db_dsn=unreachable_dsn)

    outcome = down.run_once()

    assert outcome == worker.ABANDONED
    row = row_of(conn, migration_id)
    assert (row["status"], row["error"], row["attempts"], row["lease_token"] is not None) == (
        "EXECUTING", None, 0, True,
    )
    assert events_of(conn, migration_id) == [("APPROVED", "EXECUTING", "worker:down"), REFUND_EVENT]
    detail = conn.execute(
        "SELECT detail FROM migration_events WHERE migration_id = %s ORDER BY id DESC LIMIT 1", (migration_id,)
    ).fetchone()["detail"]
    assert detail == NOT_COUNTED
    assert markers_of(target) == {}


def test_outage_longer_than_the_retry_limit_does_not_fail_the_job(conn, target, make_worker, unreachable_dsn):
    """Guarantee: with MAX_ATTEMPTS = 2, a job claimed four times while the target is
    unreachable is NOT marked FAILED. When the target is reachable again, the next claim
    runs the migration and the job ends SUCCEEDED.

    Invariant: none of the six; decision D41 (an attempt counts only if the target could be
    reached), which corrects D8 for outages. This is review finding F4(b): before, the
    third claim was "over the limit" and the job was failed without its SQL ever being tried.
    Leases are backdated between passes; nothing sleeps.
    """
    migration_id = seed_migration(conn, status="APPROVED", sql=ADD_NICKNAME)
    down = make_worker("down", target_db_dsn=unreachable_dsn, max_attempts=2)

    for _ in range(4):                                  # twice the limit
        assert down.run_once() == worker.ABANDONED
        assert row_of(conn, migration_id)["attempts"] == 0
        expire_lease(conn, migration_id)

    up = make_worker("up", max_attempts=2)              # the outage is over
    assert up.run_once() == worker.SUCCEEDED
    row = row_of(conn, migration_id)
    assert (row["status"], row["error"], row["attempts"]) == ("SUCCEEDED", None, 1)
    assert markers_of(target) == {migration_id: sha256_hex(ADD_NICKNAME)}
    assert "nickname" in columns_of(target, "customers")


def test_attempts_that_reached_the_target_still_count_across_an_unreachable_pass(
    conn, target, make_worker, unreachable_dsn
):
    """Guarantee: an unreachable pass gives back only its OWN attempt. With MAX_ATTEMPTS = 2:
    one attempt that reached the target and died there, one unreachable pass, one more
    attempt that died on the target; the next claim is over the limit and the job is marked
    FAILED, exactly as if the unreachable pass had not happened.

    Invariant: none of the six; decisions D8 and D41. The limit still stops a job that
    keeps failing; an outage in the middle neither uses the budget up nor resets it.
    How an attempt "dies on the target": a hook raises inside the target transaction, after
    the marker insert, so the transaction rolls back and the worker writes nothing.
    """
    migration_id = seed_migration(conn, status="APPROVED", sql=ADD_NICKNAME)
    up = make_worker("up", max_attempts=2)
    down = make_worker("down", target_db_dsn=unreachable_dsn, max_attempts=2)

    def die_inside_the_target_transaction():
        raise RuntimeError("simulated crash inside the target transaction")

    def real_attempt_that_dies():
        hooks.register("execute.after_marker_insert", die_inside_the_target_transaction)
        with pytest.raises(RuntimeError, match="simulated crash"):
            up.run_once()
        hooks.clear()
        expire_lease(conn, migration_id)

    real_attempt_that_dies()
    assert row_of(conn, migration_id)["attempts"] == 1

    assert down.run_once() == worker.ABANDONED          # claimed (2), refunded (1)
    assert row_of(conn, migration_id)["attempts"] == 1
    expire_lease(conn, migration_id)

    real_attempt_that_dies()
    assert row_of(conn, migration_id)["attempts"] == 2

    assert up.run_once() == worker.FAILED               # claim number 3 is over the limit
    row = row_of(conn, migration_id)
    assert (row["status"], row["error"], row["attempts"]) == ("FAILED", "retry limit reached", 3)
    assert markers_of(target) == {}


def test_stale_workers_refund_matches_zero_rows_and_leaves_the_new_owners_count_alone(
    conn, make_worker, unreachable_dsn
):
    """Guarantee: a worker that lost its job while it was failing to connect cannot lower
    the new owner's attempt count. Its refund matches 0 rows, its pass ends `lost`, and the
    row keeps the new owner's token and `attempts`; no refund event is written.

    Invariant: 3. The refund carries the old lease token, like every other worker write.
    How the race is forced: A is paused by a hook right after its claim commits, before it
    tries the target; the test expires A's lease, reclaims the job, and claims it as B; then
    A is released. No sleeps.
    """
    migration_id = seed_migration(conn, status="APPROVED", sql=ADD_NICKNAME)
    worker_a = make_worker("down", target_db_dsn=unreachable_dsn)
    a_is_paused, a_may_continue = threading.Event(), threading.Event()

    def pause():
        a_is_paused.set()
        assert a_may_continue.wait(THREAD_TIMEOUT), "test never released worker A"

    hooks.register("claim.after_commit", pause)

    thread_a = Background(worker_a.run_once)
    try:
        assert a_is_paused.wait(THREAD_TIMEOUT), "worker A never reached claim.after_commit"
        expire_lease(conn, migration_id)
        assert reclaim_expired(conn, "b") == 1
        claim_b = claim_one(conn, "b", lease_seconds=60)
    finally:
        a_may_continue.set()

    assert thread_a.join() == worker.LOST
    row = row_of(conn, migration_id)
    assert (row["status"], row["attempts"], row["lease_token"]) == ("EXECUTING", 2, claim_b.lease_token)
    assert REFUND_EVENT not in events_of(conn, migration_id)


def test_unreachable_target_at_the_retry_limit_abandons_without_a_refund(conn, make_worker, unreachable_dsn):
    """Guarantee: a job that is ALREADY over the retry limit when the target is unreachable
    is neither failed nor refunded: the pass ends `abandoned`, `attempts` is not lowered,
    and no refund event is written. It stays over the limit until the target can be asked
    whether the migration was applied (decision D39).

    Invariant: none of the six; decisions D8 and D41. The job got over the limit through
    attempts that did reach the target, so those still count; and without the target the
    worker cannot know whether to record SUCCEEDED or FAILED (DESIGN.md section 1 #8).
    """
    migration_id = seed_migration(conn, status="APPROVED", sql=ADD_NICKNAME)
    use_up_attempts(conn, migration_id, 1)
    down = make_worker("down", target_db_dsn=unreachable_dsn, max_attempts=1)

    assert down.run_once() == worker.ABANDONED

    row = row_of(conn, migration_id)
    assert (row["status"], row["error"], row["attempts"]) == ("EXECUTING", None, 2)
    assert events_of(conn, migration_id) == [("APPROVED", "EXECUTING", "worker:down")]


def test_each_refunded_pass_logs_an_error_with_the_drivers_reason(conn, settings, make_worker, caplog):
    """Guarantee: every pass that gives its attempt back logs one record at ERROR level on
    the `relay.worker` logger, naming the job and carrying the driver's reason for the
    failed connection: here, a wrong password, "password authentication failed".

    Invariant: none of the six; the condition attached to decision D41. Because an
    unreachable target is retried without limit, a permanent misconfiguration would
    otherwise be silent. The log names the cause so it is loud instead.
    (The record also carries the SQLSTATE when the driver reports one. For a failed
    connection psycopg reports none, so the text is what identifies a bad password; the
    code a server would use for it, 28P01, never reaches the client library's error.)
    """
    migration_id = seed_migration(conn, status="APPROVED", sql=ADD_NICKNAME)
    wrong_password_dsn = make_conninfo(settings.target_db_dsn, password="not-the-password")
    down = make_worker("down", target_db_dsn=wrong_password_dsn)

    with caplog.at_level(logging.ERROR, logger="relay.worker"):
        assert down.run_once() == worker.ABANDONED
        expire_lease(conn, migration_id)
        assert down.run_once() == worker.ABANDONED

    records = [r for r in caplog.records if r.name == "relay.worker" and r.levelno == logging.ERROR]
    assert len(records) == 2                                    # one per refunded pass
    for record in records:
        message = record.getMessage()
        assert str(migration_id) in message
        assert "attempt not counted" in message
        assert "password authentication failed" in message
        assert "SQLSTATE" in message
    assert row_of(conn, migration_id)["attempts"] == 0
