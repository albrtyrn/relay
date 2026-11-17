"""The transition helper, core operation 1 of 4 (DESIGN.md section 4.1, M2).

These tests call `transition()` directly, with no API in between. The caller owns the
transaction, so every call sits inside a visible `with conn.transaction():` block.
"""

import queue
import threading
import uuid

import pytest

from relay import db
from relay.transitions import LEGAL_TRANSITIONS, STATUSES, transition
from tests.helpers import (
    SEED_HASH,
    THREAD_TIMEOUT,
    Background,
    seed_migration,
    wait_until_blocked,
)

WAITING = "WAITING_FOR_APPROVAL"
# What a worker sets when it gives a job up or finishes it: the lease columns go back to NULL.
CLEAR_LEASE = {"lease_token": None, "lease_expires_at": None}


def status_of(conn, migration_id) -> str:
    return conn.execute("SELECT status FROM migrations WHERE id = %s", (migration_id,)).fetchone()["status"]


def events_of(conn, migration_id) -> list[dict]:
    return conn.execute(
        """
        SELECT from_status, to_status, actor, detail
        FROM migration_events WHERE migration_id = %s ORDER BY id
        """,
        (migration_id,),
    ).fetchall()


def test_connections_run_at_read_committed(conn):
    """Guarantee: Relay's connections use the READ COMMITTED isolation level.

    Invariant: the assumption under 1 and 3 (decision D27). The loser of a race gets "0 rows"
    only because, at READ COMMITTED, a waiting UPDATE re-checks its WHERE clause against the
    winner's committed row. At REPEATABLE READ the loser would get a serialization error (a
    500) instead of a clean 409. If someone changes the level, this test says so.
    """
    outside = conn.execute("SHOW transaction_isolation").fetchone()["transaction_isolation"]
    with conn.transaction():
        inside = conn.execute("SHOW transaction_isolation").fetchone()["transaction_isolation"]

    assert outside == "read committed"
    assert inside == "read committed"


def test_read_committed_is_pinned_not_inherited(conn):
    """Guarantee: a transaction on a Relay connection runs at READ COMMITTED even when the
    session's default says otherwise.

    Invariant: the assumption under 1 and 3 (decision D27). `db.connect` pins the level, so a
    changed server or session default cannot silently turn the loser's 409 into a
    serialization error. The test changes the default for its own session only.
    """
    conn.execute("SET default_transaction_isolation = 'repeatable read'")

    with conn.transaction():
        inside = conn.execute("SHOW transaction_isolation").fetchone()["transaction_isolation"]

    assert inside == "read committed"


def test_success_updates_status_and_writes_one_event(conn):
    """Guarantee: a matching transition changes the status, sets the extra fields, bumps
    updated_at, and writes exactly one audit event.

    Invariant: 1 (the helper is the writer of `status`) and 3 (the status change and its
    audit event are written together).
    """
    migration_id = seed_migration(conn, status=WAITING)

    with conn.transaction():
        changed = transition(
            conn, migration_id, WAITING, "APPROVED", "user:alice",
            set_fields={"approved_sql_hash": SEED_HASH},
        )

    assert changed is True
    row = conn.execute(
        "SELECT status, approved_sql_hash, updated_at > created_at AS touched FROM migrations WHERE id = %s",
        (migration_id,),
    ).fetchone()
    assert row == {"status": "APPROVED", "approved_sql_hash": SEED_HASH, "touched": True}
    assert events_of(conn, migration_id) == [
        {"from_status": WAITING, "to_status": "APPROVED", "actor": "user:alice", "detail": None}
    ]


def test_detail_is_stored_on_the_event(conn):
    """Guarantee: `detail` (a reject reason or an error text) lands on the audit event.

    Invariant: supports the audit trail (DESIGN.md section 1 #13).
    """
    migration_id = seed_migration(conn, status=WAITING)

    with conn.transaction():
        transition(conn, migration_id, WAITING, "REJECTED", "user:bob", detail="wrong table")

    assert events_of(conn, migration_id)[0]["detail"] == "wrong table"


def test_wrong_from_status_returns_false_and_writes_no_event(conn):
    """Guarantee: when the row is not in the expected status, nothing changes and no event is
    written; the helper answers False.

    Invariant: 1 and 3. This is the compare-and-set: "change it only if it is still what I
    think it is". The transition asked for here is legal; the row is just somewhere else.
    """
    migration_id = seed_migration(conn, status="APPROVED")

    with conn.transaction():
        changed = transition(
            conn, migration_id, WAITING, "APPROVED", "user:alice",
            set_fields={"approved_sql_hash": SEED_HASH},
        )

    assert changed is False
    assert status_of(conn, migration_id) == "APPROVED"
    assert events_of(conn, migration_id) == []


def test_unknown_id_returns_false_and_writes_no_event(conn):
    """Guarantee: a transition on an id that does not exist matches 0 rows and answers False.

    Invariant: 3 (no audit event without a status change).
    """
    with conn.transaction():
        changed = transition(conn, uuid.uuid4(), WAITING, "REJECTED", "user:alice")

    assert changed is False
    assert conn.execute("SELECT count(*) AS n FROM migration_events").fetchone()["n"] == 0


def test_wrong_lease_token_returns_false(conn):
    """Guarantee: a worker holding the wrong lease token cannot change the row.

    Invariant: 3. The token is the worker's proof that the job is still its own. A stale
    worker (one whose job was taken over) matches 0 rows.
    """
    migration_id = seed_migration(conn, status="DRAFTING")

    with conn.transaction():
        changed = transition(
            conn, migration_id, "DRAFTING", "FAILED", "worker:stale",
            lease_token=uuid.uuid4(),  # not the token stored on the row
            set_fields={"error": "boom", **CLEAR_LEASE},
        )

    assert changed is False
    assert status_of(conn, migration_id) == "DRAFTING"
    assert events_of(conn, migration_id) == []


def test_right_lease_token_succeeds(conn):
    """Guarantee: the worker holding the row's current lease token can change it.

    Invariant: 3. This is the other half of the token check: it refuses strangers, not owners.
    """
    migration_id = seed_migration(conn, status="DRAFTING")
    token = conn.execute("SELECT lease_token FROM migrations WHERE id = %s", (migration_id,)).fetchone()["lease_token"]

    with conn.transaction():
        changed = transition(
            conn, migration_id, "DRAFTING", "FAILED", "worker:owner",
            lease_token=token, detail="boom",
            set_fields={"error": "boom", **CLEAR_LEASE},
        )

    assert changed is True
    row = conn.execute("SELECT status, error, lease_token FROM migrations WHERE id = %s", (migration_id,)).fetchone()
    assert row == {"status": "FAILED", "error": "boom", "lease_token": None}
    assert len(events_of(conn, migration_id)) == 1


@pytest.mark.parametrize("from_status", ["DRAFTING", "EXECUTING"])
def test_leaving_leased_state_without_token_raises(conn, from_status):
    """Guarantee: code cannot move a row out of DRAFTING or EXECUTING without naming a lease
    token; the helper raises before running any SQL.

    Invariant: 3. Forgetting the token check is a bug, so it is made impossible to forget
    rather than left to each caller.
    """
    migration_id = seed_migration(conn, status=from_status)

    with conn.transaction():
        with pytest.raises(ValueError, match="lease_token"):
            transition(
                conn, migration_id, from_status, "FAILED", "worker:w1",
                set_fields={"error": "boom", **CLEAR_LEASE},
            )

    assert status_of(conn, migration_id) == from_status
    assert events_of(conn, migration_id) == []


@pytest.mark.parametrize(
    ("from_status", "to_status"),
    [
        ("PENDING", "APPROVED"),                 # skips drafting and the human
        ("PENDING", "SUCCEEDED"),
        ("WAITING_FOR_APPROVAL", "EXECUTING"),   # skips the approval
        ("WAITING_FOR_APPROVAL", "SUCCEEDED"),
        ("APPROVED", "SUCCEEDED"),               # skips execution
        ("APPROVED", "APPROVED"),                # a no-op is not a transition
        ("DRAFTING", "DRAFTING"),                # the one same-state edge is EXECUTING's (D41)
        ("PENDING", "BOGUS"),                    # not a state at all
    ],
)
def test_illegal_pair_raises(conn, from_status, to_status):
    """Guarantee: a (from, to) pair outside the legal table raises and changes nothing, even
    when the row really is in `from_status`.

    Invariant: 1. The legal table in the helper is the only definition of the workflow.
    """
    migration_id = seed_migration(conn, status=from_status)

    with conn.transaction():
        with pytest.raises(ValueError, match="illegal transition"):
            transition(conn, migration_id, from_status, to_status, "user:alice")

    assert status_of(conn, migration_id) == from_status
    assert events_of(conn, migration_id) == []


@pytest.mark.parametrize("terminal", ["SUCCEEDED", "REJECTED", "FAILED"])
def test_every_transition_out_of_terminal_states_raises(conn, terminal):
    """Guarantee: nothing leaves SUCCEEDED, REJECTED, or FAILED: every one of the eight
    targets raises, and the row keeps its status.

    Invariant: 1. Terminal means terminal (DESIGN.md section 1 #21).
    """
    migration_id = seed_migration(conn, status=terminal)

    for target in STATUSES:
        with conn.transaction():
            with pytest.raises(ValueError, match="illegal transition"):
                transition(conn, migration_id, terminal, target, "user:alice")

    assert status_of(conn, migration_id) == terminal
    assert events_of(conn, migration_id) == []


def test_same_state_executing_transition_changes_attempts_only_and_needs_the_token(conn):
    """Guarantee: the one same-state edge, EXECUTING -> EXECUTING, lets the owner of the
    lease set `attempts` while the status and the lease stay as they are, and writes its
    audit event; with another worker's token it matches 0 rows and writes nothing; with no
    token it raises before any SQL.

    Invariant: 1 and 3. This edge exists so that a worker that could not reach the target
    can give its attempt back through the helper, with the same token check and audit
    event as every other worker write (decision D41), instead of a second write path.
    """
    migration_id = seed_migration(conn, status="EXECUTING")
    before = conn.execute(
        "SELECT lease_token, lease_expires_at FROM migrations WHERE id = %s", (migration_id,)
    ).fetchone()
    conn.execute("UPDATE migrations SET attempts = 3 WHERE id = %s", (migration_id,))   # setup; not status

    with conn.transaction():
        with pytest.raises(ValueError, match="lease_token"):
            transition(conn, migration_id, "EXECUTING", "EXECUTING", "worker:w1", set_fields={"attempts": 2})
    with conn.transaction():
        stranger = transition(
            conn, migration_id, "EXECUTING", "EXECUTING", "worker:w2",
            lease_token=uuid.uuid4(), set_fields={"attempts": 2},
        )
    with conn.transaction():
        owner = transition(
            conn, migration_id, "EXECUTING", "EXECUTING", "worker:w1",
            lease_token=before["lease_token"], detail="not counted", set_fields={"attempts": 2},
        )

    assert (stranger, owner) == (False, True)
    after = conn.execute(
        "SELECT status, attempts, lease_token, lease_expires_at FROM migrations WHERE id = %s", (migration_id,)
    ).fetchone()
    assert after == {"status": "EXECUTING", "attempts": 2, **before}
    assert events_of(conn, migration_id) == [
        {"from_status": "EXECUTING", "to_status": "EXECUTING", "actor": "worker:w1", "detail": "not counted"},
    ]


def test_legal_table_is_exactly_the_eleven_designed_transitions():
    """Guarantee: the legal table holds the eleven transitions of DESIGN.md section 4.1, no
    more: the ten edges of the workflow and the one same-state edge EXECUTING -> EXECUTING
    (decision D41).

    Invariant: 1. Adding an edge to the workflow should be a deliberate change that also has
    to touch this test.
    """
    assert LEGAL_TRANSITIONS == {
        ("PENDING", "DRAFTING"),
        ("DRAFTING", "WAITING_FOR_APPROVAL"),
        ("DRAFTING", "FAILED"),
        ("DRAFTING", "PENDING"),
        ("WAITING_FOR_APPROVAL", "APPROVED"),
        ("WAITING_FOR_APPROVAL", "REJECTED"),
        ("APPROVED", "EXECUTING"),
        ("EXECUTING", "SUCCEEDED"),
        ("EXECUTING", "FAILED"),
        ("EXECUTING", "APPROVED"),
        ("EXECUTING", "EXECUTING"),
    }


@pytest.mark.parametrize("column", ["status", "description", "id", "updated_at", "no_such_column"])
def test_unknown_set_field_raises(conn, column):
    """Guarantee: `set_fields` accepts only the allow-listed columns; anything else raises
    before any SQL runs.

    Invariant: 1 (`status` cannot be smuggled in through set_fields). It is also the guard
    that keeps column names, which cannot be SQL parameters, from ever being caller-chosen text.
    """
    migration_id = seed_migration(conn, status=WAITING)

    with conn.transaction():
        with pytest.raises(ValueError, match="set_fields"):
            transition(conn, migration_id, WAITING, "REJECTED", "user:alice", set_fields={column: "x"})

    assert status_of(conn, migration_id) == WAITING
    assert events_of(conn, migration_id) == []


def test_called_outside_a_transaction_raises(conn):
    """Guarantee: the helper refuses to run when the caller has no transaction open.

    Invariant: 3 (decision D28). On an autocommit connection with no transaction, the UPDATE
    and the event INSERT would each commit on their own; a crash between them would leave a
    status change with no audit event. The helper raises instead of running that way.
    """
    migration_id = seed_migration(conn, status=WAITING)

    with pytest.raises(RuntimeError, match="transaction"):
        transition(conn, migration_id, WAITING, "REJECTED", "user:alice")

    assert status_of(conn, migration_id) == WAITING
    assert events_of(conn, migration_id) == []


def test_caller_rollback_undoes_status_and_event(conn):
    """Guarantee: if the caller's transaction rolls back after a successful transition, both
    the status change and the audit event disappear.

    Invariant: 3 (atomic with the audit event), and decision D2: the helper never commits, so
    whatever the caller wraps around it shares one fate.
    """
    migration_id = seed_migration(conn, status=WAITING)

    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        with conn.transaction():
            assert transition(conn, migration_id, WAITING, "REJECTED", "user:alice") is True
            raise Boom  # leaving the block with an exception rolls the transaction back

    assert status_of(conn, migration_id) == WAITING
    assert events_of(conn, migration_id) == []


def test_second_transaction_waits_then_matches_zero_rows(conn, settings):
    """Guarantee: when two transactions try to move the same row, the second one waits for
    the first, then matches 0 rows and writes no event.

    Invariant: 1 and 3, and the heart of "two people click at once". The race is forced, not
    hoped for: A updates the row and keeps its transaction open; B is then observed blocked on
    A's row lock; only then does A commit. At READ COMMITTED, Postgres re-checks B's WHERE
    against the committed row, `status` no longer matches, and B gets 0 rows.
    """
    migration_id = seed_migration(conn, status=WAITING)
    a_has_updated = threading.Event()
    a_may_commit = threading.Event()
    b_pid = queue.Queue()

    def session_a():
        with db.connect(settings.relay_db_dsn) as a:
            with a.transaction():
                changed = transition(
                    a, migration_id, WAITING, "APPROVED", "user:a",
                    set_fields={"approved_sql_hash": SEED_HASH},
                )
                a_has_updated.set()
                # Hold the transaction (and so the row lock) open until the test says go.
                assert a_may_commit.wait(THREAD_TIMEOUT), "test never released session A"
            return changed

    def session_b():
        with db.connect(settings.relay_db_dsn) as b:
            b_pid.put(b.info.backend_pid)
            with b.transaction():
                return transition(b, migration_id, WAITING, "REJECTED", "user:b", detail="too late")

    thread_a = Background(session_a)
    try:
        assert a_has_updated.wait(THREAD_TIMEOUT), "session A never ran its update"
        thread_b = Background(session_b)
        wait_until_blocked(conn, b_pid.get(timeout=THREAD_TIMEOUT))
        # B is stuck behind A's lock and A has not committed: nothing is visible yet.
        assert status_of(conn, migration_id) == WAITING
    finally:
        a_may_commit.set()

    assert thread_a.join() is True
    assert thread_b.join() is False
    assert status_of(conn, migration_id) == "APPROVED"
    assert events_of(conn, migration_id) == [
        {"from_status": WAITING, "to_status": "APPROVED", "actor": "user:a", "detail": None}
    ]
