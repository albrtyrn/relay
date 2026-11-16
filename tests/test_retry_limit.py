"""The retry limit (decision D8). M3 covers generation; execution cases arrive in M4."""

from relay import worker
from relay.llm.fake import FakeLLM
from tests.helpers import CountingLLM, FlakyLLM, expire_lease, seed_migration


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
