"""The claim query, core operation 2 of 4 (DESIGN.md section 4.2, M3).

A claim is how a worker takes ownership of one queued job. It picks and locks a PENDING row
(`SELECT ... FOR UPDATE SKIP LOCKED`), moves it to DRAFTING through the transition helper
with a fresh lease, and commits, all before any slow work starts.
"""

import psycopg

from relay import db
from relay.claim import claim_one
from tests.helpers import run_behind_barrier, seed_migration


def test_claim_sets_drafting_token_expiry_attempts_and_event(conn):
    """Guarantee: a claim moves the row to DRAFTING, stores a fresh lease token and an expiry
    `lease_seconds` ahead on the database clock, counts the attempt, and writes one audit
    event, and the Claim it returns carries that same token.

    Invariant: 3. The token returned here is the worker's proof of ownership for every later
    write. Status, lease, and event are set together by the transition helper (invariant 1).
    """
    migration_id = seed_migration(conn, status="PENDING", description="add column a text to customers")

    claim = claim_one(conn, "w1", lease_seconds=60)

    assert claim.id == migration_id
    assert claim.status == "DRAFTING"
    assert claim.attempts == 1
    assert claim.description == "add column a text to customers"
    row = conn.execute(
        """
        SELECT status, lease_token, attempts,
               lease_expires_at - now() BETWEEN interval '50 seconds' AND interval '60 seconds' AS expiry_ok
        FROM migrations WHERE id = %s
        """,
        (migration_id,),
    ).fetchone()
    assert row == {"status": "DRAFTING", "lease_token": claim.lease_token, "attempts": 1, "expiry_ok": True}
    events = conn.execute(
        "SELECT from_status, to_status, actor FROM migration_events WHERE migration_id = %s", (migration_id,)
    ).fetchall()
    assert events == [{"from_status": "PENDING", "to_status": "DRAFTING", "actor": "worker:w1"}]


def test_claim_on_empty_queue_returns_none(conn):
    """Guarantee: with nothing to claim, the claim returns None and writes nothing.

    Invariant: none of the six; the worker loop sleeps and polls again on None.
    """
    assert claim_one(conn, "w1", lease_seconds=60) is None
    assert conn.execute("SELECT count(*) AS n FROM migration_events").fetchone()["n"] == 0


def test_claim_ignores_rows_that_are_not_pending(conn):
    """Guarantee (M3): only PENDING rows are claimable. Rows in the other seven states,
    including APPROVED, are left alone.

    Invariant: 1 (a claim is a legal transition or nothing). APPROVED becomes claimable in
    M4, when the execute path exists; until then a worker that took an approved job would
    have nothing to do with it.
    """
    for status in ["DRAFTING", "WAITING_FOR_APPROVAL", "APPROVED", "EXECUTING", "SUCCEEDED", "REJECTED", "FAILED"]:
        seed_migration(conn, status=status)

    assert claim_one(conn, "w1", lease_seconds=60) is None


def test_claim_takes_the_oldest_pending_row_first(conn):
    """Guarantee: without contention, jobs are claimed oldest first.

    Invariant: none of the six. (Under contention the order is only approximate: SKIP LOCKED
    passes over a locked older row. DESIGN.md section 4.2, "Does not guarantee".)
    """
    first = seed_migration(conn, status="PENDING")
    second = seed_migration(conn, status="PENDING")

    assert [claim_one(conn, "w1", 60).id, claim_one(conn, "w1", 60).id] == [first, second]


def test_two_claimers_one_row_second_gets_none(conn, settings):
    """Guarantee: while one claimer's transaction holds a row, a second claimer does not wait
    for it and does not take it: it gets None at once.

    Invariant: 3 (never two owners). How the race is forced: A's claim is wrapped in an outer
    transaction that stays open, so A's row lock is still held when B claims. (Inside an open
    transaction, `claim_one`'s own `with conn.transaction()` becomes a savepoint, a nested
    step that commits only when the outer transaction does.) If B waited instead of skipping,
    this test would hang and then fail on B's lock timeout.
    """
    migration_id = seed_migration(conn, status="PENDING")

    with db.connect(settings.relay_db_dsn) as a, db.connect(settings.relay_db_dsn) as b:
        b.execute("SET lock_timeout = '3s'")  # only matters if B wrongly waits
        with a.transaction():
            claim_a = claim_one(a, "worker-a", lease_seconds=60)

            claim_b = claim_one(b, "worker-b", lease_seconds=60)

            assert claim_a.id == migration_id
            assert claim_b is None
            # A has not committed yet: everyone else still sees the row as PENDING.
            assert conn.execute("SELECT status FROM migrations WHERE id = %s", (migration_id,)).fetchone()["status"] == "PENDING"

    events = conn.execute("SELECT actor FROM migration_events WHERE migration_id = %s", (migration_id,)).fetchall()
    assert events == [{"actor": "worker:worker-a"}]


def test_second_claimer_skips_the_locked_row_and_takes_the_next(conn, settings):
    """Guarantee: SKIP LOCKED means "pass over", not "give up": with two rows queued and the
    older one locked by A, B claims the newer one.

    Invariant: 3. Workers never queue behind each other for the same job.
    """
    older = seed_migration(conn, status="PENDING")
    newer = seed_migration(conn, status="PENDING")

    with db.connect(settings.relay_db_dsn) as a, db.connect(settings.relay_db_dsn) as b:
        b.execute("SET lock_timeout = '3s'")  # only matters if B wrongly waits: fail, don't hang
        with a.transaction():
            claim_a = claim_one(a, "worker-a", lease_seconds=60)
            claim_b = claim_one(b, "worker-b", lease_seconds=60)

    assert (claim_a.id, claim_b.id) == (older, newer)


def test_four_claimers_50_rows_each_row_claimed_exactly_once(conn, settings):
    """Guarantee: 4 claimers released together over 50 queued rows hand out exactly 50
    claims, no row twice, and every row ends up holding the token of the one claim made on it.

    Invariant: 3 (never two valid leases on one job). This counts claims directly, as the
    values `claim_one` returned, separately from the audit events that the worker-level test
    counts. The race is forced with a Barrier, not sleeps.
    """
    ids = {seed_migration(conn, status="PENDING") for _ in range(50)}

    def claim_until_empty(name):
        def action(own_conn: psycopg.Connection):
            claims = []
            while (claim := claim_one(own_conn, name, lease_seconds=60)) is not None:
                claims.append(claim)
            return claims
        return action

    per_claimer = run_behind_barrier(settings.relay_db_dsn, [claim_until_empty(f"w{n}") for n in range(4)])

    claims = [claim for claims in per_claimer for claim in claims]
    assert len(claims) == 50
    assert {claim.id for claim in claims} == ids            # every row, and no row twice
    assert len({claim.lease_token for claim in claims}) == 50
    stored = conn.execute("SELECT id, lease_token, status, attempts FROM migrations").fetchall()
    assert {(row["id"], row["lease_token"]) for row in stored} == {(c.id, c.lease_token) for c in claims}
    assert all(row["status"] == "DRAFTING" and row["attempts"] == 1 for row in stored)
