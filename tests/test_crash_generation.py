"""A worker process dies right after claiming a job (DESIGN.md section 8, M3)."""

from relay import worker
from tests.helpers import THREAD_TIMEOUT, expire_lease, seed_migration, spawn_worker


def test_worker_killed_after_claim_is_reclaimed_and_job_completes(conn, settings, make_worker):
    """Guarantee: a job whose worker dies after claiming it is not lost: once the lease
    expires, another worker reclaims it and finishes it.

    Invariant: 3, plus the "claim commits before work" half of invariant 2: the dead worker
    left a committed DRAFTING row with a lease, which is exactly what makes recovery possible.
    How the crash is forced: a real `python -m relay.worker` process is started with
    RELAY_HOOK=claim.after_commit:crash, which calls os._exit(137) right after the claim
    commits. No cleanup code runs, as with `kill -9`. The lease is then backdated, not waited out.
    """
    migration_id = seed_migration(conn, status="PENDING", description="add column nickname text to customers")

    process = spawn_worker(settings, worker_id="crasher", hook="claim.after_commit:crash")
    output, _ = process.communicate(timeout=THREAD_TIMEOUT * 3)
    assert process.returncode == 137, f"worker did not crash at the hook; output:\n{output}"

    # Mid-crash state: claimed, leased, and abandoned. Nothing was drafted.
    mid = conn.execute(
        "SELECT status, lease_token IS NOT NULL AS leased, attempts, proposed_sql FROM migrations WHERE id = %s",
        (migration_id,),
    ).fetchone()
    assert mid == {"status": "DRAFTING", "leased": True, "attempts": 1, "proposed_sql": None}

    survivor = make_worker("survivor")
    assert survivor.run_once() == worker.IDLE      # the dead worker's lease is still live
    expire_lease(conn, migration_id)
    assert survivor.run_once() == worker.SAVED     # reclaim, claim, draft, save

    final = conn.execute(
        "SELECT status, proposed_sql, lease_token FROM migrations WHERE id = %s", (migration_id,)
    ).fetchone()
    assert final == {
        "status": "WAITING_FOR_APPROVAL",
        "proposed_sql": "ALTER TABLE public.customers ADD COLUMN nickname text",
        "lease_token": None,
    }
    events = conn.execute(
        "SELECT from_status, to_status, actor FROM migration_events WHERE migration_id = %s ORDER BY id",
        (migration_id,),
    ).fetchall()
    assert [(e["from_status"], e["to_status"], e["actor"]) for e in events] == [
        ("PENDING", "DRAFTING", "worker:crasher"),
        ("DRAFTING", "PENDING", "reclaimer:survivor"),
        ("PENDING", "DRAFTING", "worker:survivor"),
        ("DRAFTING", "WAITING_FOR_APPROVAL", "worker:survivor"),
    ]
