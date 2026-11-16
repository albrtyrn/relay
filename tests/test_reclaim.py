"""Lease-expiry reclaim (DESIGN.md section 4.5; drafting in M3, executing in M4).

A lease is a time-limited claim. When it runs out without the job finishing (the worker
crashed, hung, or gave up), any worker may return the job to the queue it came from:
DRAFTING -> PENDING, or EXECUTING -> APPROVED.
"""

from relay import hooks, worker
from relay.claim import reclaim_expired
from tests.helpers import expire_lease, seed_migration


def row_of(conn, migration_id) -> dict:
    return conn.execute(
        "SELECT status, lease_token, lease_expires_at, attempts FROM migrations WHERE id = %s", (migration_id,)
    ).fetchone()


def events_of(conn, migration_id) -> list[tuple]:
    rows = conn.execute(
        "SELECT from_status, to_status, actor FROM migration_events WHERE migration_id = %s ORDER BY id",
        (migration_id,),
    ).fetchall()
    return [(row["from_status"], row["to_status"], row["actor"]) for row in rows]


def test_expired_drafting_returns_to_pending_with_reclaimer_event(conn):
    """Guarantee: a DRAFTING row whose lease has expired goes back to PENDING with its lease
    cleared, and the audit log names the reclaimer.

    Invariant: 1 and 3. Reclaim goes through the transition helper, guarded by the expired
    token it read under a row lock: "I am ending exactly this lease" (DESIGN.md section 1 #4).
    """
    migration_id = seed_migration(conn, status="DRAFTING")
    expire_lease(conn, migration_id)

    reclaimed = reclaim_expired(conn, "w2")

    assert reclaimed == 1
    assert row_of(conn, migration_id) == {
        "status": "PENDING", "lease_token": None, "lease_expires_at": None, "attempts": 0,
    }
    assert events_of(conn, migration_id) == [("DRAFTING", "PENDING", "reclaimer:w2")]


def test_expired_executing_returns_to_approved(conn):
    """Guarantee: an EXECUTING row whose lease has expired goes back to APPROVED (not to
    PENDING, and not to FAILED) with its lease cleared, its approval kept, and a reclaimer
    event in the audit log.

    Invariant: 1 and 3, as for drafting. Reclaim never judges the earlier attempt: it cannot
    know whether the dead worker committed on the target. It only puts the job back in the
    queue; the next worker's marker insert finds out (DESIGN.md section 4.5).
    """
    migration_id = seed_migration(conn, status="EXECUTING")
    expire_lease(conn, migration_id)

    reclaimed = reclaim_expired(conn, "w2")

    assert reclaimed == 1
    assert row_of(conn, migration_id) == {
        "status": "APPROVED", "lease_token": None, "lease_expires_at": None, "attempts": 0,
    }
    assert events_of(conn, migration_id) == [("EXECUTING", "APPROVED", "reclaimer:w2")]
    approved = conn.execute("SELECT approved_sql_hash FROM migrations WHERE id = %s", (migration_id,)).fetchone()
    assert approved["approved_sql_hash"] is not None


def test_unexpired_lease_is_not_reclaimed(conn):
    """Guarantee: a lease that has not expired is left alone, in both leased states.

    Invariant: 3. A live lease means a worker may still be working; taking the job away
    would create two owners.
    """
    drafting = seed_migration(conn, status="DRAFTING")      # seeded with a far-future expiry
    executing = seed_migration(conn, status="EXECUTING")
    before = (row_of(conn, drafting), row_of(conn, executing))

    reclaimed = reclaim_expired(conn, "w2")

    assert reclaimed == 0
    assert (row_of(conn, drafting), row_of(conn, executing)) == before
    assert conn.execute("SELECT count(*) AS n FROM migration_events").fetchone()["n"] == 0


def test_reclaim_keeps_the_attempt_count(conn, make_worker):
    """Guarantee: reclaiming does not reset `attempts`; the next claim counts on from it.

    Invariant: supports the retry limit (decision D8): a job that keeps killing its worker
    must not look brand new each time.
    """
    migration_id = seed_migration(conn, status="PENDING", description="fake:transient")
    w1 = make_worker("w1")

    w1.run_once()                       # claim (attempts 1), transient error, lease abandoned
    expire_lease(conn, migration_id)
    reclaim_expired(conn, "w2")

    assert row_of(conn, migration_id)["attempts"] == 1


def test_owner_can_finish_after_expiry_if_not_yet_reclaimed(conn, make_worker):
    """Guarantee: a worker whose lease expired but was not reclaimed can still save its work.

    Invariant: 3 and decision D6. Expiry gives others permission to take the job; it does
    not by itself take it away. Until someone reclaims the row, the token on it is still the
    owner's and nobody else can be working on it, so accepting the late write is safe.
    The lease is expired at an exact point (a hook that fires after the LLM returned and
    before the save), not by waiting.
    """
    migration_id = seed_migration(conn, status="PENDING", description="add column nickname text to customers")
    hooks.register("draft.before_save", lambda: expire_lease(conn, migration_id))

    outcome = make_worker("w1").run_once()

    assert outcome == worker.SAVED
    assert row_of(conn, migration_id)["status"] == "WAITING_FOR_APPROVAL"
