"""Reclaim under concurrency (DESIGN.md section 4.5; added after the review, REVIEW.md section 5).

`test_reclaim.py` shows reclaim one step after another. These tests make two parties reach
the SAME expired row at the SAME moment:

- two reclaimers (two workers both sweeping for expired leases), and
- a reclaimer against the slow owner's final write.

Why both are safe, in one paragraph: both sides must take the same row lock (a row lock is
Postgres's "one writer at a time" rule for a single row). The sweep uses `SKIP LOCKED`, so
it passes over a row someone else holds instead of queueing for it. The owner's `UPDATE`,
if it arrives second, waits for the lock and then re-checks its own `WHERE` against the
new row version (READ COMMITTED behavior), finds the status or token changed, and matches
0 rows.

Two kinds of test, as in DESIGN.md section 7.3:

- FORCED: one side sits inside a transaction the test keeps open, so its row lock is
  certainly held when the other side acts. No sleeps, no luck.
- START-TOGETHER: many threads are released at once by a Barrier. Nothing forces a
  particular order, so these assert something that must hold in EVERY order.
"""

import threading

import pytest

from relay import db, hooks, worker
from relay.claim import reclaim_expired
from relay.sqlhash import sha256_hex
from relay.transitions import transition
from tests.helpers import (
    THREAD_TIMEOUT,
    Background,
    events_of,
    expire_lease,
    run_behind_barrier,
    seed_migration,
    wait_until_blocked,
)

DRAFT_SQL = "ALTER TABLE public.customers ADD COLUMN nickname text"
ADD_NICKNAME_DESCRIPTION = "add column nickname text to customers"   # what the fake LLM turns into DRAFT_SQL

# What a drafting owner's final write sets (DESIGN.md section 4.1, DRAFTING -> WAITING_FOR_APPROVAL).
SAVE_FIELDS = {
    "proposed_sql": DRAFT_SQL,
    "proposed_sql_hash": sha256_hex(DRAFT_SQL),
    "risk_level": "LOW",
    "risk_reason": "test proposal",
    "attempts": 0,
    "lease_token": None,
    "lease_expires_at": None,
}

# A sweep that wrongly WAITS for a locked row (instead of skipping it) would wait on a lock
# held by this very test, forever. This bound turns that hang into a quick, clear failure.
SWEEP_LOCK_TIMEOUT = "2s"


def status_of(conn, migration_id) -> str:
    return conn.execute("SELECT status FROM migrations WHERE id = %s", (migration_id,)).fetchone()["status"]


def token_of(conn, migration_id):
    return conn.execute(
        "SELECT lease_token FROM migrations WHERE id = %s", (migration_id,)
    ).fetchone()["lease_token"]


def pause_at(hook_name: str):
    """Arm `hook_name` so the worker that reaches it stops there until the test says go.

    Returns (is_paused, may_continue): the test waits on the first and sets the second.
    """
    is_paused, may_continue = threading.Event(), threading.Event()

    def pause():
        is_paused.set()
        assert may_continue.wait(THREAD_TIMEOUT), "test never released the paused worker"

    hooks.register(hook_name, pause)
    return is_paused, may_continue


# ---------------------------------------------------------------- two reclaimers, one row


def test_second_reclaimer_skips_a_row_the_first_is_reclaiming(conn, settings):
    """Guarantee: while reclaimer R1's sweep holds an expired row (not yet committed), R2's
    sweep returns 0 at once, without waiting. After R1 commits the row is PENDING with
    exactly one reclaim event.

    Invariant: 3 (one lease is ended once, by one writer, with one audit event), and the
    liveness of reclaim: a sweep never queues behind another sweep. `SKIP LOCKED` in the
    sweep is what this test protects: without it R2 would wait for R1's lock and fail here
    on its lock timeout.
    How the race is forced: R1's sweep runs inside an outer transaction the test keeps open,
    so R1's row lock is held at the moment R2 sweeps.
    """
    migration_id = seed_migration(conn, status="DRAFTING")
    expire_lease(conn, migration_id)

    with db.connect(settings.relay_db_dsn) as r1, db.connect(settings.relay_db_dsn) as r2:
        r2.execute(f"SET lock_timeout = '{SWEEP_LOCK_TIMEOUT}'")
        with r1.transaction():
            assert reclaim_expired(r1, "r1") == 1
            assert reclaim_expired(r2, "r2") == 0                   # skipped; did not wait
            assert status_of(conn, migration_id) == "DRAFTING"      # R1 has not committed yet

    assert status_of(conn, migration_id) == "PENDING"
    assert events_of(conn, migration_id) == [("DRAFTING", "PENDING", "reclaimer:r1")]


def test_eight_reclaimers_40_expired_rows_each_reclaimed_exactly_once(conn, settings):
    """Guarantee: 8 reclaimers sweeping 40 expired rows (20 DRAFTING, 20 EXECUTING) at once
    reclaim 40 in total: every row gets exactly one reclaim event and no lease survives.

    Invariant: 3. A lease is ended once; no row is reclaimed twice and none is missed.
    START-TOGETHER, not forced: a Barrier releases the 8 threads at once and each sweeps
    (10 rows per sweep) until it finds nothing. The assertion holds in every interleaving,
    so luck cannot make this pass wrongly; it could only miss a rare bug.
    """
    migration_ids = []
    for n in range(40):
        migration_id = seed_migration(conn, status="DRAFTING" if n % 2 else "EXECUTING")
        expire_lease(conn, migration_id)
        migration_ids.append(migration_id)

    def sweep_until_empty(name):
        def action(own):
            total = 0
            while (count := reclaim_expired(own, name)) > 0:
                total += count
            return total
        return action

    counts = run_behind_barrier(settings.relay_db_dsn, [sweep_until_empty(f"r{n}") for n in range(8)])

    assert sum(counts) == 40
    events_per_row = conn.execute(
        "SELECT migration_id, count(*) AS n FROM migration_events GROUP BY migration_id"
    ).fetchall()
    assert {row["migration_id"]: row["n"] for row in events_per_row} == {mid: 1 for mid in migration_ids}
    leases_left = conn.execute("SELECT count(*) AS n FROM migrations WHERE lease_token IS NOT NULL").fetchone()["n"]
    assert leases_left == 0
    statuses = conn.execute("SELECT status, count(*) AS n FROM migrations GROUP BY status").fetchall()
    assert {row["status"]: row["n"] for row in statuses} == {"PENDING": 20, "APPROVED": 20}


# ---------------------------------------------------------------- reclaimer vs. the owner's save


def test_reclaimer_skips_a_row_whose_owner_is_saving(conn, settings):
    """Guarantee: while the owner's final write is in flight (its transaction open, the row
    locked), a reclaim sweep skips the row; the owner's save then stands, with one event.

    Invariant: 3, and DESIGN.md section 4.5: "an expired lease that nobody has reclaimed yet
    still belongs to its owner". Also protects `SKIP LOCKED` in the sweep: without it the
    reclaimer would wait behind the owner and fail here on its lock timeout.
    How the race is forced: the owner's transition runs inside a transaction the test keeps
    open while the reclaimer sweeps.
    """
    migration_id = seed_migration(conn, status="DRAFTING")
    token = token_of(conn, migration_id)
    expire_lease(conn, migration_id)

    with db.connect(settings.relay_db_dsn) as owner, db.connect(settings.relay_db_dsn) as reclaimer:
        reclaimer.execute(f"SET lock_timeout = '{SWEEP_LOCK_TIMEOUT}'")
        with owner.transaction():
            assert transition(
                owner, migration_id, "DRAFTING", "WAITING_FOR_APPROVAL", "worker:owner",
                lease_token=token, set_fields=SAVE_FIELDS,
            )
            assert reclaim_expired(reclaimer, "r") == 0             # skipped; did not wait

    assert status_of(conn, migration_id) == "WAITING_FOR_APPROVAL"
    assert events_of(conn, migration_id) == [("DRAFTING", "WAITING_FOR_APPROVAL", "worker:owner")]


def test_owner_save_waits_for_an_open_reclaim_then_loses_when_it_commits(conn, settings, make_worker):
    """Guarantee: if a reclaim of the row is in flight when the slow owner tries to save, the
    owner's write WAITS; once the reclaim commits, the write matches 0 rows. The owner's pass
    ends `lost`, the row is PENDING, and the audit log holds no save.

    Invariant: 3. The owner's UPDATE carries `status = DRAFTING AND lease_token = mine`; after
    the wait Postgres re-checks that against the committed row (now PENDING, no token) and
    it no longer matches.
    How the race is forced, through the real worker: A is paused by a hook at
    `draft.before_save`; the test expires A's lease and reclaims the row inside a transaction
    it keeps open; A is released and is OBSERVED blocked on the row lock
    (`wait_until_blocked`); only then does the reclaim commit.
    """
    migration_id = seed_migration(conn, status="PENDING", description=ADD_NICKNAME_DESCRIPTION)
    worker_a = make_worker("a")
    a_is_paused, a_may_continue = pause_at("draft.before_save")

    thread_a = Background(worker_a.run_once)
    try:
        assert a_is_paused.wait(THREAD_TIMEOUT), "worker A never reached draft.before_save"
        expire_lease(conn, migration_id)
        with db.connect(settings.relay_db_dsn) as reclaimer:
            with reclaimer.transaction():
                assert reclaim_expired(reclaimer, "r") == 1
                a_may_continue.set()
                wait_until_blocked(conn, worker_a.conn.info.backend_pid)   # A really waits on the row
                assert status_of(conn, migration_id) == "DRAFTING"          # reclaim not committed yet
    finally:
        a_may_continue.set()

    assert thread_a.join() == worker.LOST
    assert status_of(conn, migration_id) == "PENDING"
    assert events_of(conn, migration_id) == [
        ("PENDING", "DRAFTING", "worker:a"),
        ("DRAFTING", "PENDING", "reclaimer:r"),
    ]


def test_owner_save_proceeds_when_the_open_reclaim_rolls_back(conn, settings, make_worker):
    """Guarantee: the same race, but the reclaim transaction rolls back instead of
    committing (its process died mid-sweep). The waiting owner's save then goes through: the
    row is WAITING_FOR_APPROVAL and the audit log holds no reclaim.

    Invariant: 3, and DESIGN.md section 4.5 "crash mid-sweep: the transaction rolls back".
    A reclaim that never committed ended no lease, so the owner still owns the row.
    How the race is forced: as in the test above; the test raises inside the reclaim's
    transaction block after seeing A blocked, which rolls it back.
    """
    migration_id = seed_migration(conn, status="PENDING", description=ADD_NICKNAME_DESCRIPTION)
    worker_a = make_worker("a")
    a_is_paused, a_may_continue = pause_at("draft.before_save")

    class ReclaimerDied(Exception):
        pass

    thread_a = Background(worker_a.run_once)
    try:
        assert a_is_paused.wait(THREAD_TIMEOUT), "worker A never reached draft.before_save"
        expire_lease(conn, migration_id)
        with db.connect(settings.relay_db_dsn) as reclaimer:
            with pytest.raises(ReclaimerDied):
                with reclaimer.transaction():
                    assert reclaim_expired(reclaimer, "r") == 1
                    a_may_continue.set()
                    wait_until_blocked(conn, worker_a.conn.info.backend_pid)
                    raise ReclaimerDied   # leaving the block by exception rolls the reclaim back
    finally:
        a_may_continue.set()

    assert thread_a.join() == worker.SAVED
    assert status_of(conn, migration_id) == "WAITING_FOR_APPROVAL"
    assert events_of(conn, migration_id) == [
        ("PENDING", "DRAFTING", "worker:a"),
        ("DRAFTING", "WAITING_FOR_APPROVAL", "worker:a"),
    ]


def test_owner_saves_and_three_reclaimers_on_30_rows_one_outcome_per_row(conn, settings):
    """Guarantee: with one owner saving 30 expired DRAFTING rows while 3 reclaimers sweep
    them, every row ends with exactly ONE event, a status that matches that event, and a
    result the owner was told truthfully (its write returned True exactly for the rows that
    ended WAITING_FOR_APPROVAL).

    Invariant: 3. For each row either the owner's save or a reclaim wins, never both and
    never neither.
    START-TOGETHER, not forced: a Barrier releases the 4 threads at once. How many rows each
    side wins varies from run to run (it may even be 30 to 0); the assertions hold for any
    split, so this adds breadth to the three forced tests above rather than replacing them.
    """
    rows = []
    for _ in range(30):
        migration_id = seed_migration(conn, status="DRAFTING")
        rows.append((migration_id, token_of(conn, migration_id)))
        expire_lease(conn, migration_id)

    def owner(own):
        saved = {}
        for migration_id, token in rows:
            with own.transaction():
                saved[migration_id] = transition(
                    own, migration_id, "DRAFTING", "WAITING_FOR_APPROVAL", "worker:owner",
                    lease_token=token, set_fields=SAVE_FIELDS,
                )
        return saved

    def sweeper(name):
        def action(own):
            total = 0
            for _ in range(200):
                total += reclaim_expired(own, name, batch_size=3)
            return total
        return action

    results = run_behind_barrier(settings.relay_db_dsn, [owner, sweeper("r1"), sweeper("r2"), sweeper("r3")])
    saved_by_owner, reclaimed = results[0], sum(results[1:])

    assert sum(1 for ok in saved_by_owner.values() if ok) + reclaimed == 30
    for migration_id, _ in rows:
        events = events_of(conn, migration_id)
        assert len(events) == 1
        status = status_of(conn, migration_id)
        assert status == events[0][1]
        assert (status == "WAITING_FOR_APPROVAL") == saved_by_owner[migration_id]
