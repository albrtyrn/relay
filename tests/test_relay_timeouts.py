"""Timeouts on Relay's own database connections (decision D40; review finding F3).

Invariant 2 says: never hold a transaction or row lock across slow work. The code obeys it
for PLANNED slow work (the LLM call, the target transaction). These tests are about
UNPLANNED slow work: a process that freezes, or loses its network, in the middle of one of
Relay's short transactions. Without a limit, its row lock stays for as long as its
connection does, and everyone who needs that row waits with it.

Two Postgres settings, set on every connection opened with `db.connect_relay`:

- idle_in_transaction_session_timeout (RELAY_IDLE_TIMEOUT, default 10s): the SERVER ends a
  session that sits inside an open transaction doing nothing, and rolls it back. This
  removes the frozen lock HOLDER.
- lock_timeout (RELAY_LOCK_TIMEOUT, default 5s): a statement that waits too long for a lock
  gets an error instead of waiting forever. This frees the one who is WAITING.

What each party does with a lock timeout (nothing was changed by the statement that got it):

- the API answers 503 with `Retry-After: 1`: "busy, nothing happened, try again";
- a worker abandons its lease, like every other temporary problem (decision D7).

"Idle in transaction" is the important phrase. A connection that is merely idle, with NO
transaction open, is never touched: the last three tests show a healthy worker waiting on
slow work far longer than the idle timeout without being hit, because Relay connections are
autocommit and nothing is open between its short transactions.

About time in these tests: several really wait 300 ms to 1 s for a real timeout or a slow
call. That is the thing under test, not a sleep used to create a race. The blocking
situations themselves are forced by transactions the test keeps open.
"""

import threading
import time
from dataclasses import replace

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.pq import TransactionStatus

from relay import db, hooks, service, worker
from relay.api import create_app
from relay.claim import claim_one, reclaim_expired
from relay.config import load_settings
from relay.llm import Proposal
from relay.sqlhash import sha256_hex
from relay.transitions import transition
from tests.helpers import (
    SEED_HASH,
    THREAD_TIMEOUT,
    Background,
    columns_of,
    events_of,
    expire_lease,
    markers_of,
    seed_migration,
    wait_until_blocked,
    wait_until_session_is_gone,
)

ADD_NICKNAME = "ALTER TABLE public.customers ADD COLUMN nickname text"
ADD_NICKNAME_DESCRIPTION = "add column nickname text to customers"
SHORT = "300ms"
# For the two "frozen holder" tests. Long enough that the other side is certainly seen
# blocked before the server ends the frozen session; short enough to keep the test quick.
FROZEN_IDLE_TIMEOUT = "1s"


def setting_of(conn, name: str) -> str:
    return conn.execute("SELECT current_setting(%s) AS value", (name,)).fetchone()["value"]


def row_of(conn, migration_id) -> dict:
    return conn.execute(
        "SELECT status, error, lease_token FROM migrations WHERE id = %s", (migration_id,)
    ).fetchone()


def pause_at(hook_name: str):
    """Arm `hook_name` so the worker that reaches it stops there until the test says go."""
    is_paused, may_continue = threading.Event(), threading.Event()

    def pause():
        is_paused.set()
        assert may_continue.wait(THREAD_TIMEOUT), "test never released the paused worker"

    hooks.register(hook_name, pause)
    return is_paused, may_continue


class SlowLLM:
    """A test LLM that takes `seconds` to answer, like a real model call."""

    def __init__(self, seconds: float):
        self._seconds = seconds

    def propose(self, description: str, schema_summary: str) -> Proposal:
        time.sleep(self._seconds)    # the slow work itself, not a way to line up a race
        return Proposal(sql=ADD_NICKNAME, risk_level="LOW", risk_reason="slow test proposal")


# ---------------------------------------------------------------- the settings are in force


def test_relay_connections_carry_both_timeouts_and_plain_connections_do_not(settings):
    """Guarantee: a connection opened with `db.connect_relay` has lock_timeout and
    idle_in_transaction_session_timeout set for the whole session, from its first statement;
    a plain `db.connect` connection (the target, dbinit, the tests' own) has neither.

    Invariant: 2, extended to unplanned stalls (decision D40). The target sets its timeouts
    per transaction instead (`test_timeouts_apply_inside_the_target_transaction_only`).
    """
    with db.connect_relay(settings) as relay_conn, db.connect(settings.relay_db_dsn) as plain:
        assert setting_of(relay_conn, "lock_timeout") == "5s"
        assert setting_of(relay_conn, "idle_in_transaction_session_timeout") == "10s"
        assert setting_of(plain, "lock_timeout") == "0"
        assert setting_of(plain, "idle_in_transaction_session_timeout") == "0"


def test_relay_connections_are_autocommit_so_a_plain_read_leaves_no_transaction_open(settings):
    """Guarantee: a `db.connect_relay` connection is autocommit: after a statement outside
    an explicit `with conn.transaction():` block, NO transaction is open.

    Invariant: 2. This is what keeps the idle timeout away from healthy code. In psycopg's
    default mode a single SELECT silently opens a transaction and leaves it open; such a
    connection would be "idle in transaction" during the next slow call and the server
    would end it.
    """
    with db.connect_relay(settings) as relay_conn:
        assert relay_conn.autocommit is True
        relay_conn.execute("SELECT count(*) FROM migrations").fetchone()
        assert relay_conn.info.transaction_status == TransactionStatus.IDLE


@pytest.mark.parametrize("field", ["relay_lock_timeout", "relay_idle_timeout"])
@pytest.mark.parametrize("bad_value", ["0s", "5 seconds", ""])
def test_zero_or_malformed_relay_timeout_is_refused(settings, field, bad_value):
    """Guarantee: `db.connect_relay` refuses a timeout of zero or one not written as
    <whole number><ms|s|min>, before opening any connection.

    Invariant: 2. In Postgres 0 means "no timeout", which would silently switch the
    protection off; and the value is placed in the connection's startup options, so only
    the checked format is ever sent.
    """
    with pytest.raises(ValueError):
        db.connect_relay(replace(settings, **{field: bad_value}))


def test_relay_timeouts_are_read_from_the_environment():
    """Guarantee: RELAY_LOCK_TIMEOUT and RELAY_IDLE_TIMEOUT are read from the environment,
    with defaults of 5s and 10s when unset.

    Invariant: none of the six; configuration (DESIGN.md section 7.6).
    """
    base = {"RELAY_DB_DSN": "relay", "TARGET_DB_DSN": "target"}
    defaults = load_settings(base)
    custom = load_settings({**base, "RELAY_LOCK_TIMEOUT": "2s", "RELAY_IDLE_TIMEOUT": "1min"})

    assert (defaults.relay_lock_timeout, defaults.relay_idle_timeout) == ("5s", "10s")
    assert (custom.relay_lock_timeout, custom.relay_idle_timeout) == ("2s", "1min")


# ---------------------------------------------------------------- the frozen lock holder is removed


def test_frozen_reclaimer_is_ended_by_the_server_and_the_blocked_owner_proceeds(conn, settings):
    """Guarantee: a session frozen inside a reclaim transaction (row locked, no COMMIT) is
    ended by the server after the idle timeout and its transaction rolled back. The owner's
    final write, which was blocked behind it, then goes through.

    Invariant: 2 for unplanned stalls, and 3 (after the wait, the owner's UPDATE re-checks
    its WHERE: the reclaim never committed, so status and token still match). This was
    review finding F3, sequence A: before, the owner waited for as long as the frozen
    process stayed connected.
    How it is forced: the "frozen" session is a Relay connection whose reclaim sweep runs
    inside a transaction the test never commits; the owner is OBSERVED blocked on the row
    lock; then the test waits for the server to end the frozen session.
    """
    migration_id = seed_migration(conn, status="DRAFTING")
    token = conn.execute("SELECT lease_token FROM migrations WHERE id = %s", (migration_id,)).fetchone()["lease_token"]
    expire_lease(conn, migration_id)
    frozen_settings = replace(settings, relay_idle_timeout=FROZEN_IDLE_TIMEOUT)

    def owner_fails_its_job(owner):
        with owner.transaction():
            return transition(
                owner, migration_id, "DRAFTING", "FAILED", "worker:owner", lease_token=token,
                set_fields={"error": "test", "lease_token": None, "lease_expires_at": None},
            )

    with db.connect_relay(frozen_settings) as frozen, db.connect(settings.relay_db_dsn) as owner:
        frozen_pid = frozen.info.backend_pid
        # Leaving this block tries to COMMIT on a session the server has already ended,
        # which raises: that is the frozen process "waking up" to a dead connection.
        with pytest.raises(psycopg.Error):
            with frozen.transaction():
                assert reclaim_expired(frozen, "frozen") == 1
                thread = Background(lambda: owner_fails_its_job(owner))
                wait_until_blocked(conn, owner.info.backend_pid)      # the owner really waits
                wait_until_session_is_gone(conn, frozen_pid)          # the idle timeout fired
                assert thread.join() is True                          # and the owner got through

    assert row_of(conn, migration_id)["status"] == "FAILED"
    assert events_of(conn, migration_id) == [("DRAFTING", "FAILED", "worker:owner")]


def test_frozen_claimer_is_ended_by_the_server_and_the_job_becomes_claimable_again(conn, settings):
    """Guarantee: a session frozen inside a claim transaction (row locked FOR UPDATE, no
    COMMIT) is ended by the server after the idle timeout; the job is then still PENDING,
    unlocked, and another worker claims it.

    Invariant: 2 for unplanned stalls. This was review finding F3, sequence C, the one the
    lease cannot cover: an uncommitted claim has no lease yet, so the row is PENDING with
    nothing to expire, and while it is locked every claimer skips it.
    How it is forced: the claim runs inside an outer transaction the test never commits.
    """
    migration_id = seed_migration(conn, status="PENDING")
    frozen_settings = replace(settings, relay_idle_timeout=FROZEN_IDLE_TIMEOUT)

    with db.connect_relay(frozen_settings) as frozen:
        frozen_pid = frozen.info.backend_pid
        with pytest.raises(psycopg.Error):       # the COMMIT of a session that no longer exists
            with frozen.transaction():
                assert claim_one(frozen, "frozen", lease_seconds=60) is not None   # uncommitted
                assert claim_one(conn, "other", lease_seconds=60) is None          # locked: skipped
                wait_until_session_is_gone(conn, frozen_pid)

    assert row_of(conn, migration_id)["status"] == "PENDING"       # the frozen claim was rolled back
    claim = claim_one(conn, "other", lease_seconds=60)
    assert claim is not None and claim.id == migration_id
    assert events_of(conn, migration_id) == [("PENDING", "DRAFTING", "worker:other")]


# ---------------------------------------------------------------- the waiter is freed: worker


def test_draft_save_that_times_out_on_a_row_lock_abandons_and_the_retry_saves(conn, settings, make_worker):
    """Guarantee: when a worker's draft save waits longer than RELAY_LOCK_TIMEOUT for the
    row (someone holds it in an open transaction), the worker writes nothing, raises
    nothing, and ends the pass as `abandoned`. The job stays DRAFTING; after the lease
    expires the normal retry drafts and saves it.

    Invariant: 3 (no partial write: the timed-out UPDATE and its audit event roll back
    together) and decision D7 (one retry mechanism: abandon the lease).
    How it is forced: A is paused at `draft.before_save`; the test reclaims A's expired row
    inside a transaction it keeps open, releases A (lock timeout 300 ms), and only rolls its
    own transaction back after A has given up.
    """
    migration_id = seed_migration(conn, status="PENDING", description=ADD_NICKNAME_DESCRIPTION)
    worker_a = make_worker("a", relay_lock_timeout=SHORT)
    a_is_paused, a_may_continue = pause_at("draft.before_save")

    thread_a = Background(worker_a.run_once)
    try:
        assert a_is_paused.wait(THREAD_TIMEOUT), "worker A never reached draft.before_save"
        expire_lease(conn, migration_id)
        with db.connect(settings.relay_db_dsn) as holder:
            with holder.transaction():
                assert reclaim_expired(holder, "holder") == 1      # holds the row, uncommitted
                a_may_continue.set()
                assert thread_a.join() == worker.ABANDONED         # A waited 300 ms and gave up
                raise psycopg.Rollback                             # the holder goes away
    finally:
        a_may_continue.set()

    row = row_of(conn, migration_id)
    assert (row["status"], row["error"], row["lease_token"] is not None) == ("DRAFTING", None, True)
    assert events_of(conn, migration_id) == [("PENDING", "DRAFTING", "worker:a")]

    hooks.clear()
    assert worker_a.run_once() == worker.SAVED     # reclaims its own expired lease, drafts again
    assert row_of(conn, migration_id)["status"] == "WAITING_FOR_APPROVAL"


def test_succeeded_write_that_times_out_on_a_row_lock_abandons_and_the_next_worker_finds_the_marker(
    conn, target, settings, make_worker
):
    """Guarantee: when a worker has COMMITTED on the target and its SUCCEEDED write then
    waits longer than RELAY_LOCK_TIMEOUT for the row, the pass ends `abandoned` with nothing
    written to Relay. The next worker finds the marker, runs no second DDL, and records
    SUCCEEDED.

    Invariant: 4 (the committed marker is what makes abandoning safe here) and 3.
    How it is forced: A is paused at `execute.after_target_commit`; the test holds A's
    expired row in an open reclaim transaction while A (lock timeout 300 ms) tries to write.
    """
    migration_id = seed_migration(conn, status="APPROVED", sql=ADD_NICKNAME)
    worker_a = make_worker("a", relay_lock_timeout=SHORT)
    a_is_paused, a_may_continue = pause_at("execute.after_target_commit")

    thread_a = Background(worker_a.run_once)
    try:
        assert a_is_paused.wait(THREAD_TIMEOUT), "worker A never reached execute.after_target_commit"
        expire_lease(conn, migration_id)
        with db.connect(settings.relay_db_dsn) as holder:
            with holder.transaction():
                assert reclaim_expired(holder, "holder") == 1
                a_may_continue.set()
                assert thread_a.join() == worker.ABANDONED
                raise psycopg.Rollback
    finally:
        a_may_continue.set()

    assert row_of(conn, migration_id)["status"] == "EXECUTING"
    assert markers_of(target) == {migration_id: sha256_hex(ADD_NICKNAME)}

    hooks.clear()
    assert make_worker("b").run_once() == worker.SUCCEEDED
    assert row_of(conn, migration_id)["status"] == "SUCCEEDED"
    assert events_of(conn, migration_id) == [
        ("APPROVED", "EXECUTING", "worker:a"),
        ("EXECUTING", "APPROVED", "reclaimer:b"),
        ("APPROVED", "EXECUTING", "worker:b"),
        ("EXECUTING", "SUCCEEDED", "worker:b"),
    ]


# ---------------------------------------------------------------- the waiter is freed: API


def test_approve_that_times_out_on_a_row_lock_gets_503_retry_after_and_changes_nothing(conn, settings):
    """Guarantee: when an approve waits longer than RELAY_LOCK_TIMEOUT because another
    decision on the same migration is still uncommitted, the API answers 503 with
    `Retry-After: 1` and a plain message, with no stack trace or driver text. Nothing was
    changed: the row is still WAITING_FOR_APPROVAL with no event, and the same approve
    succeeds once the other request is gone.

    Invariant: 3 (the timed-out UPDATE and its event roll back together). Why 503 and not
    409: 409 would say "someone else decided", but the other request has not committed and
    may still roll back (here it does). 503 says what is known: busy, nothing happened,
    retry (DESIGN.md section 1 #26).
    How it is forced: the test makes a reject inside a transaction it keeps open, sends the
    approve (lock timeout 300 ms) against it, then rolls its reject back.
    """
    migration_id = seed_migration(conn, status="WAITING_FOR_APPROVAL")
    client = TestClient(create_app(replace(settings, relay_lock_timeout=SHORT)))
    approve = {"json": {"sql_hash": SEED_HASH}, "headers": {"X-User": "ada"}}

    with db.connect(settings.relay_db_dsn) as other:
        with other.transaction():
            status_code, _body = service.reject(other, migration_id, SEED_HASH, "not now", actor="user:bob")
            assert status_code == 200                                    # uncommitted
            # In a Background thread: if the lock timeout ever stopped firing, the approve
            # would wait on a lock this test itself holds. The join's time limit turns that
            # into a failure instead of a test that hangs forever.
            response = Background(
                lambda: client.post(f"/migrations/{migration_id}/approve", **approve)
            ).join()
            raise psycopg.Rollback                                       # bob's request dies

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    assert response.json() == {
        "detail": "migration is busy: another request is changing it; nothing was changed, retry"
    }
    assert "Traceback" not in response.text and "psycopg" not in response.text
    assert row_of(conn, migration_id)["status"] == "WAITING_FOR_APPROVAL"
    assert events_of(conn, migration_id) == []

    retry = client.post(f"/migrations/{migration_id}/approve", **approve)
    assert retry.status_code == 200
    assert retry.json()["status"] == "APPROVED"


# ---------------------------------------------------------------- a healthy worker is never hit


def test_llm_call_longer_than_the_idle_timeout_does_not_cost_the_worker_its_connection(conn, make_worker):
    """Guarantee: a draft whose LLM call takes 1 s still saves when RELAY_IDLE_TIMEOUT is
    300 ms: the idle timeout does not touch a healthy worker.

    Invariant: 2. The timeout is "idle IN TRANSACTION". The claim commits before the LLM is
    called and the connection is autocommit, so during the call the worker's Relay session
    is idle with no transaction open, which the timeout ignores. If any read had left a
    transaction open, the server would end the session mid-call and the save would fail.
    """
    migration_id = seed_migration(conn, status="PENDING")
    w1 = make_worker("w1", llm=SlowLLM(1.0), relay_idle_timeout=SHORT)

    assert w1.run_once() == worker.SAVED

    assert row_of(conn, migration_id)["status"] == "WAITING_FOR_APPROVAL"
    assert w1.conn.info.transaction_status == TransactionStatus.IDLE    # same session, still alive


def test_target_work_longer_than_the_idle_timeout_does_not_cost_the_worker_its_connection(
    conn, target, make_worker
):
    """Guarantee: an execution whose target transaction takes 1 s still ends SUCCEEDED when
    RELAY_IDLE_TIMEOUT is 300 ms.

    Invariant: 2. Before the target transaction the worker reads its row on Relay once (the
    lease re-check, decision D33). That read is a plain autocommit statement, so it leaves
    no Relay transaction open behind it while the target work runs.
    How the slow target work is made: a hook inside the target transaction, after the marker
    insert, waits 1 s. (The target's own idle timeout is its 30 s default.)
    """
    migration_id = seed_migration(conn, status="APPROVED", sql=ADD_NICKNAME)
    w1 = make_worker("w1", relay_idle_timeout=SHORT)
    hooks.register("execute.after_marker_insert", lambda: time.sleep(1.0))   # slow work, not a race

    assert w1.run_once() == worker.SUCCEEDED

    assert row_of(conn, migration_id)["status"] == "SUCCEEDED"
    assert "nickname" in columns_of(target, "customers")


def test_worker_idle_between_passes_longer_than_the_idle_timeout_keeps_its_connection(conn, make_worker):
    """Guarantee: a worker that finds nothing to do, waits 1 s, and looks again still has a
    working connection when RELAY_IDLE_TIMEOUT is 300 ms.

    Invariant: 2. The reclaim sweep and the claim each COMMIT before returning, so a worker
    sleeping between polls is idle, not idle in a transaction.
    """
    w1 = make_worker("w1", relay_idle_timeout=SHORT)

    assert w1.run_once() == worker.IDLE
    time.sleep(1.0)                                    # the poll wait itself, not a race
    migration_id = seed_migration(conn, status="PENDING", description=ADD_NICKNAME_DESCRIPTION)
    assert w1.run_once() == worker.SAVED
    assert row_of(conn, migration_id)["status"] == "WAITING_FOR_APPROVAL"
