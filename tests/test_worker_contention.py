"""Several workers draining one queue at the same time (DESIGN.md section 8).

M3 covers generation; the execution case arrives in M4.
"""

from collections import Counter

from relay import worker
from relay.llm.fake import FakeLLM
from tests.helpers import CountingLLM, run_behind_barrier, seed_migration


def test_generation_50_jobs_4_workers(conn, settings, make_worker):
    """Guarantee: 4 workers released together over 50 PENDING jobs bring all 50 to
    WAITING_FOR_APPROVAL, and each job is claimed once and drafted once.

    Invariant: 3 (never two valid leases on one job). Claims are counted separately from the
    final state, two ways: exactly one PENDING -> DRAFTING audit event per job, and exactly
    one LLM call per job. (The values `claim_one` itself returns are counted in
    test_claim.py::test_four_claimers_50_rows_each_row_claimed_exactly_once.)
    The race is forced with a Barrier: all 4 workers are connected and waiting before any acts.
    """
    descriptions = [f"add column c{n} text to customers" for n in range(50)]
    ids = [seed_migration(conn, status="PENDING", description=d) for d in descriptions]
    llm = CountingLLM(FakeLLM())
    workers = [make_worker(f"w{n}", llm=llm) for n in range(4)]

    def drain(one_worker):
        def action(_unused_conn):
            outcomes = []
            # IDLE means nothing is left that this worker could claim. A row locked by
            # another worker's claim is being taken by that worker, so stopping is safe.
            while (outcome := one_worker.run_once()) != worker.IDLE:
                outcomes.append(outcome)
            return outcomes
        return action

    per_worker = run_behind_barrier(settings.relay_db_dsn, [drain(w) for w in workers])

    outcomes = [outcome for outcomes in per_worker for outcome in outcomes]
    assert Counter(outcomes) == {worker.SAVED: 50}

    rows = conn.execute("SELECT id, status, lease_token, attempts FROM migrations").fetchall()
    assert {row["id"] for row in rows} == set(ids)
    assert all(
        (row["status"], row["lease_token"], row["attempts"]) == ("WAITING_FOR_APPROVAL", None, 0)
        for row in rows
    )

    claim_events = conn.execute(
        """
        SELECT migration_id, count(*) AS n FROM migration_events
        WHERE from_status = 'PENDING' AND to_status = 'DRAFTING'
        GROUP BY migration_id
        """
    ).fetchall()
    assert {row["migration_id"]: row["n"] for row in claim_events} == {migration_id: 1 for migration_id in ids}

    assert Counter(llm.calls) == {description: 1 for description in descriptions}
    # Exactly two events per job (claim, save): no reclaims and no duplicate saves.
    assert conn.execute("SELECT count(*) AS n FROM migration_events").fetchone()["n"] == 100
