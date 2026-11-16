"""The retry limit (decision D8): generation cases (M3) and execution cases (M4)."""

import pytest

from relay import hooks, worker
from relay.llm.fake import FakeLLM
from relay.sqlhash import sha256_hex
from tests.helpers import (
    CountingLLM,
    FlakyLLM,
    columns_of,
    expire_lease,
    markers_of,
    raise_once,
    seed_migration,
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

    Invariant: 4, and decision D8: at the limit the worker reads the marker before deciding,
    so a migration that really was applied is never recorded as failed. Setup: with
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

    Invariant: 3 (the FAILED write is made by the worker holding the claim). Setup: with
    MAX_ATTEMPTS = 1, attempt 1 "dies" right after its claim, before any target work (a hook
    raises at `claim.after_commit`); attempt 2 is over the limit.
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
