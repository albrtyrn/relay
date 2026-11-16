"""A worker process dies after the target commit and before telling Relay
(DESIGN.md section 4.3, "Crash after target commit"; M4).

This is the case the marker exists for. The two databases cannot commit together, so there
is always a moment when the target says "applied" and Relay still says EXECUTING. A worker
that dies in that moment must not cause the DDL to run a second time.
"""

from relay import worker
from relay.sqlhash import sha256_hex
from tests.helpers import (
    THREAD_TIMEOUT,
    columns_of,
    events_of,
    expire_lease,
    markers_of,
    read_until,
    seed_migration,
    spawn_worker,
)

ADD_NICKNAME = "ALTER TABLE public.customers ADD COLUMN nickname text"
HOOK = "execute.after_target_commit"


def status_of(conn, migration_id) -> dict:
    return conn.execute(
        "SELECT status, lease_token IS NOT NULL AS leased, attempts FROM migrations WHERE id = %s",
        (migration_id,),
    ).fetchone()


def assert_survivor_finishes_without_second_ddl(conn, target, make_worker, migration_id, dead_worker: str):
    """The shared second half of both tests: the mid-crash state, then the recovery."""
    # Mid-crash state: the target has the marker and the column; Relay still says EXECUTING.
    assert markers_of(target) == {migration_id: sha256_hex(ADD_NICKNAME)}
    assert "nickname" in columns_of(target, "customers")
    assert status_of(conn, migration_id) == {"status": "EXECUTING", "leased": True, "attempts": 1}

    survivor = make_worker("survivor")
    assert survivor.run_once() == worker.IDLE          # the dead worker's lease is still live
    expire_lease(conn, migration_id)
    assert survivor.run_once() == worker.SUCCEEDED     # reclaim, claim, find the marker, skip the DDL

    # Had the survivor run the DDL again, the target would have rejected it ("column already
    # exists") and the job would be FAILED. SUCCEEDED with still one marker shows it skipped.
    assert status_of(conn, migration_id) == {"status": "SUCCEEDED", "leased": False, "attempts": 2}
    assert markers_of(target) == {migration_id: sha256_hex(ADD_NICKNAME)}
    assert events_of(conn, migration_id) == [
        ("APPROVED", "EXECUTING", f"worker:{dead_worker}"),
        ("EXECUTING", "APPROVED", "reclaimer:survivor"),
        ("APPROVED", "EXECUTING", "worker:survivor"),
        ("EXECUTING", "SUCCEEDED", "worker:survivor"),
    ]


def test_next_worker_finds_marker_skips_ddl_and_marks_succeeded(conn, target, settings, make_worker):
    """Guarantee: a worker killed right after the target commit leaves an applied migration
    that Relay still shows as EXECUTING; once the lease expires, the next worker finds the
    matching marker, does NOT run the DDL again, and marks the job SUCCEEDED.

    Invariant: 4 (an existing marker with the approved hash counts as success) and 3 (the
    survivor's write uses its own fresh token). How the crash is forced: a real
    `python -m relay.worker` process runs with RELAY_HOOK=execute.after_target_commit:crash,
    which calls os._exit(137) at that line: no cleanup, like `kill -9`. The lease is
    backdated, not waited out.
    """
    migration_id = seed_migration(conn, status="APPROVED", sql=ADD_NICKNAME)

    process = spawn_worker(settings, worker_id="crasher", hook=f"{HOOK}:crash")
    output, _ = process.communicate(timeout=THREAD_TIMEOUT * 3)
    assert process.returncode == 137, f"worker did not crash at the hook; output:\n{output}"

    assert_survivor_finishes_without_second_ddl(conn, target, make_worker, migration_id, dead_worker="crasher")


def test_worker_paused_after_target_commit_then_killed_is_recovered(conn, target, settings, make_worker):
    """Guarantee: the same recovery holds when the worker is frozen at that point and then
    killed from outside (SIGKILL), which is what scripts/demo_crash.py shows.

    Invariant: 4 and 3, as above. This is also the test for the `:pause` form of the
    environment-variable hook: the worker prints `PAUSED at <name>` and stays alive, holding
    its lease, until something kills it.
    """
    migration_id = seed_migration(conn, status="APPROVED", sql=ADD_NICKNAME)

    process = spawn_worker(settings, worker_id="frozen", hook=f"{HOOK}:pause")
    try:
        output = read_until(process, f"PAUSED at {HOOK}")
        assert process.poll() is None, f"worker should still be running; output:\n{output}"
    finally:
        process.kill()                                  # SIGKILL: no cleanup code runs
    process.wait(timeout=THREAD_TIMEOUT)

    assert_survivor_finishes_without_second_ddl(conn, target, make_worker, migration_id, dead_worker="frozen")
