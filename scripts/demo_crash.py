"""Demo: a worker dies after changing the target and before telling Relay.

    docker compose run --rm tests python -m scripts.demo_crash

What it shows, step by step:

    1. A migration is created, drafted, and approved.
    2. Worker A runs it on the target, then FREEZES right after the target commit
       (RELAY_HOOK=execute.after_target_commit:pause), before it can tell Relay.
    3. The two databases now disagree: the target has the new column and its marker,
       Relay still says EXECUTING.
    4. Worker A is killed (SIGKILL: no cleanup code runs).
    5. Worker B can do nothing while A's lease is live. When the lease expires, B reclaims
       the job, finds A's marker on the target, skips the DDL, and marks the job SUCCEEDED.

The same sequence is asserted by
tests/test_crash_after_target_commit.py::test_worker_paused_after_target_commit_then_killed_is_recovered.
This script exists so you can watch it happen; the test is what proves it.

It uses its own databases, `relay_demo` and `target_demo`, rebuilt on every run, so it never
touches dev data and the Compose workers cannot take its job. Unlike the tests, it waits out
a real (short) lease instead of backdating it.
"""

import os
import subprocess
import sys
import time

from psycopg.conninfo import make_conninfo

from relay import db, repo, service, worker
from relay.config import Settings, load_admin_dsn, load_migrator_password
from relay.dbinit import MIGRATOR_ROLE, init_databases
from relay.llm.fake import FakeLLM

RELAY_DEMO_DB = "relay_demo"
TARGET_DEMO_DB = "target_demo"
HOOK = "execute.after_target_commit"
DESCRIPTION = "add column loyalty_tier text to customers"
NEW_COLUMN = "loyalty_tier"

# A short lease so the demo does not take a minute. The worker refuses to start unless the
# lease is longer than lock_timeout + statement_timeout + a 10 s margin (decision D33), so
# the two timeouts are shortened to match: 15 > 1 + 2 + 10.
LEASE_SECONDS = 15
LOCK_TIMEOUT = "1s"
STATEMENT_TIMEOUT = "2s"


def say(text: str = "") -> None:
    print(text, flush=True)


def show_state(relay_conn, target_conn, migration_id) -> dict:
    """Print, and return, what each database currently says about the migration."""
    row = relay_conn.execute(
        "SELECT status, attempts, lease_expires_at > now() AS lease_live FROM migrations WHERE id = %s",
        (migration_id,),
    ).fetchone()
    markers = target_conn.execute(
        "SELECT count(*) AS n FROM relay_applied WHERE migration_id = %s", (migration_id,)
    ).fetchone()["n"]
    has_column = target_conn.execute(
        """
        SELECT count(*) AS n FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'customers' AND column_name = %s
        """,
        (NEW_COLUMN,),
    ).fetchone()["n"] == 1
    lease = {None: "none", True: "live", False: "expired"}[row["lease_live"]]
    say(f"     Relay : status = {row['status']}, attempts = {row['attempts']}, lease = {lease}")
    say(f"     Target: markers for this migration = {markers}, column customers.{NEW_COLUMN} exists = {has_column}")
    return {"status": row["status"], "markers": markers, "has_column": has_column}


def start_frozen_worker(settings: Settings) -> subprocess.Popen:
    """Start worker A as a real process that will freeze right after its target commit."""
    env = {
        **os.environ,
        "RELAY_DB_DSN": settings.relay_db_dsn,
        "TARGET_DB_DSN": settings.target_db_dsn,
        "WORKER_ID": "worker-a",
        "LEASE_SECONDS": str(settings.lease_seconds),
        "LOCK_TIMEOUT": settings.lock_timeout,
        "STATEMENT_TIMEOUT": settings.statement_timeout,
        "POLL_SECONDS": "0.2",
        "RELAY_HOOK": f"{HOOK}:pause",
    }
    return subprocess.Popen(
        [sys.executable, "-m", "relay.worker"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )


def wait_for_pause(process: subprocess.Popen) -> None:
    """Read worker A's output until it reports that it is frozen at the hook."""
    for line in process.stdout:
        if f"PAUSED at {HOOK}" in line:
            return
    raise SystemExit("demo failed: worker A exited without reaching the pause point")


def main() -> int:
    admin_dsn = load_admin_dsn()
    migrator_password = load_migrator_password()
    init_databases(admin_dsn, RELAY_DEMO_DB, TARGET_DEMO_DB, migrator_password, reset=True)
    settings = Settings(
        relay_db_dsn=make_conninfo(admin_dsn, dbname=RELAY_DEMO_DB),
        target_db_dsn=make_conninfo(
            admin_dsn, dbname=TARGET_DEMO_DB, user=MIGRATOR_ROLE, password=migrator_password
        ),
        lease_seconds=LEASE_SECONDS,
        lock_timeout=LOCK_TIMEOUT,
        statement_timeout=STATEMENT_TIMEOUT,
    )
    relay_conn = db.connect(settings.relay_db_dsn)
    target_conn = db.connect(make_conninfo(admin_dsn, dbname=TARGET_DEMO_DB))
    worker_b = worker.Worker(settings, FakeLLM(), worker_id="worker-b")
    process = None
    try:
        say("1. Create a migration, let a worker draft it, and approve it.")
        migration = repo.create_migration(relay_conn, DESCRIPTION, "user:demo")
        migration_id = migration["id"]
        assert worker_b.run_once() == worker.SAVED
        proposal = repo.get_migration(relay_conn, migration_id)
        say(f"     proposed SQL: {proposal['proposed_sql']}")
        with relay_conn.transaction():
            status_code, _body = service.approve(relay_conn, migration_id, proposal["proposed_sql_hash"], "user:demo")
        assert status_code == 200
        show_state(relay_conn, target_conn, migration_id)

        say()
        say("2. Worker A runs it and freezes right after the TARGET commit, before telling Relay.")
        process = start_frozen_worker(settings)
        wait_for_pause(process)

        say()
        say("3. The two databases now disagree. This is the moment the marker exists for:")
        frozen = show_state(relay_conn, target_conn, migration_id)

        say()
        say("4. Kill worker A (SIGKILL). It never gets to tell Relay.")
        process.kill()
        process.wait(timeout=10)
        say(f"     worker A exit code: {process.returncode}")

        say()
        say("5. Worker B looks for work. A's lease is still live, so there is nothing to take:")
        say(f"     worker B: {worker_b.run_once()}")
        say(f"   Waiting for A's {LEASE_SECONDS}-second lease to expire...")
        outcome = worker.IDLE
        deadline = time.monotonic() + LEASE_SECONDS + 15
        while outcome == worker.IDLE and time.monotonic() < deadline:
            time.sleep(1)
            outcome = worker_b.run_once()
        say(f"     worker B: {outcome}  (reclaimed the job, found A's marker, skipped the DDL)")

        say()
        say("6. Final state:")
        final = show_state(relay_conn, target_conn, migration_id)
        say("   Audit trail:")
        for event in repo.get_events(relay_conn, migration_id):
            came_from = event["from_status"] or "(created)"
            say(f"     {came_from:<21} -> {event['to_status']:<21} by {event['actor']}")

        ok = (
            frozen == {"status": "EXECUTING", "markers": 1, "has_column": True}
            and outcome == worker.SUCCEEDED
            and final == {"status": "SUCCEEDED", "markers": 1, "has_column": True}
        )
        say()
        say(
            "RESULT: the migration was applied once and recorded as SUCCEEDED, although the worker "
            "that applied it died before it could say so."
            if ok else "RESULT: UNEXPECTED STATE. The demo did not behave as described above."
        )
        return 0 if ok else 1
    finally:
        if process is not None and process.poll() is None:
            process.kill()
        worker_b.close()
        relay_conn.close()
        target_conn.close()


if __name__ == "__main__":
    sys.exit(main())
