"""A stale worker: one whose job was taken over while it was still working (DESIGN.md section 8).

Two drafting cases (M3) and the executing case (M4).
"""

import threading

from relay import hooks, worker
from relay.claim import claim_one, reclaim_expired
from relay.sqlhash import sha256_hex
from tests.helpers import (
    THREAD_TIMEOUT,
    Background,
    FixedLLM,
    events_of,
    expire_lease,
    markers_of,
    seed_migration,
)

SQL_FROM_A = "ALTER TABLE public.customers ADD COLUMN from_a text"
SQL_FROM_B = "ALTER TABLE public.customers ADD COLUMN from_b text"


def test_stale_drafter_write_matches_zero_rows(conn, make_worker):
    """Guarantee: worker A's lease expires and worker B reclaims, re-claims, and finishes the
    job; when A then tries to save, its write matches 0 rows. The row and the audit log show
    only B's result.

    Invariant: 3 (every worker write checks the current lease_token and expected status).
    How the race is forced: A is paused by a hook at the exact line between "LLM returned"
    and "save" (`draft.before_save`). While A stands there the test expires its lease and
    runs B to completion, then releases A. No sleeps.
    """
    migration_id = seed_migration(conn, status="PENDING")
    worker_a = make_worker("a", llm=FixedLLM(SQL_FROM_A))
    worker_b = make_worker("b", llm=FixedLLM(SQL_FROM_B))

    a_is_paused = threading.Event()
    a_may_continue = threading.Event()
    already_paused_someone = []

    def pause_first_caller_only():
        # Hooks are process-wide, and B runs through the same line later. Only the first
        # caller (A) is paused; B passes straight through.
        if already_paused_someone:
            return
        already_paused_someone.append(True)
        a_is_paused.set()
        assert a_may_continue.wait(THREAD_TIMEOUT), "test never released worker A"

    hooks.register("draft.before_save", pause_first_caller_only)

    thread_a = Background(worker_a.run_once)
    try:
        assert a_is_paused.wait(THREAD_TIMEOUT), "worker A never reached draft.before_save"
        expire_lease(conn, migration_id)
        outcome_b = worker_b.run_once()   # reclaims A's expired lease, claims, drafts, saves
    finally:
        a_may_continue.set()
    outcome_a = thread_a.join()

    assert outcome_b == worker.SAVED
    assert outcome_a == worker.LOST
    row = conn.execute(
        "SELECT status, proposed_sql, lease_token FROM migrations WHERE id = %s", (migration_id,)
    ).fetchone()
    assert row == {"status": "WAITING_FOR_APPROVAL", "proposed_sql": SQL_FROM_B, "lease_token": None}
    events = conn.execute(
        "SELECT from_status, to_status, actor FROM migration_events WHERE migration_id = %s ORDER BY id",
        (migration_id,),
    ).fetchall()
    assert [(e["from_status"], e["to_status"], e["actor"]) for e in events] == [
        ("PENDING", "DRAFTING", "worker:a"),
        ("DRAFTING", "PENDING", "reclaimer:b"),
        ("PENDING", "DRAFTING", "worker:b"),
        ("DRAFTING", "WAITING_FOR_APPROVAL", "worker:b"),
        # and nothing from worker:a after its claim
    ]


def test_stale_executor_write_matches_zero_rows(conn, target, make_worker):
    """Guarantee: worker A commits on the target and is then overtaken: its lease expires and
    worker B reclaims, re-claims, finds A's marker, and marks the job SUCCEEDED. When A
    finally tries to write SUCCEEDED itself, the write matches 0 rows. The DDL ran once and
    the audit log holds exactly one success, B's.

    Invariant: 3 (A's write is refused: the status is no longer EXECUTING and the token is
    no longer A's) and 4 (B treats the matching marker as success and skips the DDL).
    How the race is forced: A is paused by a hook at `execute.after_target_commit`, between
    its target commit and its Relay write. No sleeps.
    """
    sql = "ALTER TABLE public.customers ADD COLUMN nickname text"
    migration_id = seed_migration(conn, status="APPROVED", sql=sql)
    worker_a = make_worker("a")
    worker_b = make_worker("b")

    a_is_paused = threading.Event()
    a_may_continue = threading.Event()
    already_paused_someone = []

    def pause_first_caller_only():
        if already_paused_someone:      # B passes the same line later; let it through
            return
        already_paused_someone.append(True)
        a_is_paused.set()
        assert a_may_continue.wait(THREAD_TIMEOUT), "test never released worker A"

    hooks.register("execute.after_target_commit", pause_first_caller_only)

    thread_a = Background(worker_a.run_once)
    try:
        assert a_is_paused.wait(THREAD_TIMEOUT), "worker A never reached execute.after_target_commit"
        expire_lease(conn, migration_id)
        outcome_b = worker_b.run_once()   # reclaims, claims, finds the marker, marks SUCCEEDED
    finally:
        a_may_continue.set()
    outcome_a = thread_a.join()

    assert outcome_b == worker.SUCCEEDED
    assert outcome_a == worker.LOST
    assert markers_of(target) == {migration_id: sha256_hex(sql)}
    row = conn.execute("SELECT status, lease_token FROM migrations WHERE id = %s", (migration_id,)).fetchone()
    assert row == {"status": "SUCCEEDED", "lease_token": None}
    assert events_of(conn, migration_id) == [
        ("APPROVED", "EXECUTING", "worker:a"),
        ("EXECUTING", "APPROVED", "reclaimer:b"),
        ("APPROVED", "EXECUTING", "worker:b"),
        ("EXECUTING", "SUCCEEDED", "worker:b"),
        # and nothing from worker:a after its claim
    ]


def test_stale_drafter_cannot_overwrite_a_new_owner_mid_draft(conn, make_worker):
    """Guarantee: when the job has been reclaimed and re-claimed by B, but B has NOT finished,
    A's save still matches 0 rows: the row stays DRAFTING under B's token, with no proposal.

    Invariant: 3, and specifically the lease_token half of it. In this state the status is
    DRAFTING for both workers, so the status check alone would let A through; only the token
    tells the old owner from the new one. A is paused at `draft.before_save` by a hook while
    the test expires its lease and makes B's claim.
    """
    migration_id = seed_migration(conn, status="PENDING")
    worker_a = make_worker("a", llm=FixedLLM(SQL_FROM_A))
    a_is_paused = threading.Event()
    a_may_continue = threading.Event()

    def pause():
        a_is_paused.set()
        assert a_may_continue.wait(THREAD_TIMEOUT), "test never released worker A"

    hooks.register("draft.before_save", pause)

    thread_a = Background(worker_a.run_once)
    try:
        assert a_is_paused.wait(THREAD_TIMEOUT), "worker A never reached draft.before_save"
        expire_lease(conn, migration_id)
        assert reclaim_expired(conn, "b") == 1
        claim_b = claim_one(conn, "b", lease_seconds=60)   # B owns the job and is "still drafting"
    finally:
        a_may_continue.set()
    outcome_a = thread_a.join()

    assert outcome_a == worker.LOST
    row = conn.execute(
        "SELECT status, lease_token, proposed_sql FROM migrations WHERE id = %s", (migration_id,)
    ).fetchone()
    assert row == {"status": "DRAFTING", "lease_token": claim_b.lease_token, "proposed_sql": None}
