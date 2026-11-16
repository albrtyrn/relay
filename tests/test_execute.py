"""The execute path: APPROVED -> EXECUTING -> target transaction -> SUCCEEDED or FAILED
(DESIGN.md sections 4.3 and 5, M4).

The target transaction is "marker first": it inserts a row into `relay_applied` (the marker,
a record that says "this migration was applied here") BEFORE the DDL, in the same
transaction. Marker and DDL therefore commit together or not at all, and a later attempt
can tell from the marker alone whether the DDL already ran.
"""

import uuid

import psycopg
import pytest

from relay import hooks, target as target_ops, worker
from relay.sqlhash import sha256_hex
from tests.helpers import (
    columns_of,
    events_of,
    expire_lease,
    insert_marker,
    markers_of,
    seed_migration,
    set_lease_remaining,
)

ADD_NICKNAME = "ALTER TABLE public.customers ADD COLUMN nickname text"
ORIGINAL_CUSTOMER_COLUMNS = {"id", "email", "full_name", "created_at"}


def row_of(conn, migration_id) -> dict:
    return conn.execute(
        "SELECT status, error, lease_token, attempts FROM migrations WHERE id = %s", (migration_id,)
    ).fetchone()


def test_happy_path_adds_column_writes_marker_marks_succeeded(conn, target, make_worker):
    """Guarantee: an approved migration is run on the target exactly as approved: the column
    exists, one marker carries the approved hash, and Relay says SUCCEEDED with the lease
    cleared and two audit events (claim, success).

    Invariant: 4 (marker and DDL in one target transaction) and 3 (the SUCCEEDED write is
    guarded by the worker's lease token and commits with its audit event).
    """
    migration_id = seed_migration(conn, status="APPROVED", sql=ADD_NICKNAME)

    outcome = make_worker("w1").run_once()

    assert outcome == worker.SUCCEEDED
    assert columns_of(target, "customers") == ORIGINAL_CUSTOMER_COLUMNS | {"nickname"}
    assert markers_of(target) == {migration_id: sha256_hex(ADD_NICKNAME)}
    assert row_of(conn, migration_id) == {"status": "SUCCEEDED", "error": None, "lease_token": None, "attempts": 1}
    assert events_of(conn, migration_id) == [
        ("APPROVED", "EXECUTING", "worker:w1"),
        ("EXECUTING", "SUCCEEDED", "worker:w1"),
    ]


def test_failed_ddl_leaves_schema_unchanged_no_marker_status_failed_with_error(conn, target, make_worker):
    """Guarantee: when the target rejects the DDL, nothing of the attempt remains there (no
    schema change and NO marker), and Relay says FAILED with the database's error saved on
    the row and on the audit event.

    Invariant: 4. The marker was inserted before the DDL in the same transaction, so the
    DDL's failure rolls the marker back with it. A marker left behind would make a later
    attempt believe the migration had been applied. The failing SQL is allowed by the policy
    but adds a column that already exists (DESIGN.md section 1 #12).
    """
    duplicate_column = "ALTER TABLE public.customers ADD COLUMN email text"
    migration_id = seed_migration(conn, status="APPROVED", sql=duplicate_column)

    outcome = make_worker("w1").run_once()

    assert outcome == worker.FAILED
    assert columns_of(target, "customers") == ORIGINAL_CUSTOMER_COLUMNS
    assert markers_of(target) == {}
    row = row_of(conn, migration_id)
    assert (row["status"], row["lease_token"]) == ("FAILED", None)
    assert "42701" in row["error"]                    # SQLSTATE for "duplicate column"
    assert 'column "email" of relation "customers" already exists' in row["error"]
    event = conn.execute(
        "SELECT detail FROM migration_events WHERE migration_id = %s AND to_status = 'FAILED'", (migration_id,)
    ).fetchone()
    assert event["detail"] == row["error"]


def test_sql_not_matching_approved_hash_fails_without_touching_target(conn, target, make_worker):
    """Guarantee: if the stored SQL is not the SQL that was approved (its hash differs from
    `approved_sql_hash`), the worker marks the job FAILED and never opens a target transaction.

    Invariant: 4 ("only for the approved hash"). An approval is for one exact statement;
    anything else is unapproved SQL however it got there.
    """
    migration_id = seed_migration(
        conn, status="APPROVED", sql=ADD_NICKNAME, approved_hash=sha256_hex("some other statement")
    )

    outcome = make_worker("w1").run_once()

    assert outcome == worker.FAILED
    assert row_of(conn, migration_id)["error"] == "stored SQL does not match the approved hash"
    assert columns_of(target, "customers") == ORIGINAL_CUSTOMER_COLUMNS
    assert markers_of(target) == {}


def test_policy_recheck_failure_fails_without_touching_target(conn, target, make_worker):
    """Guarantee: the SQL policy runs again just before execution; SQL it rejects is marked
    FAILED and never reaches the target, even though it was "approved" with a matching hash.

    Invariant: none of the six; defense in depth (DESIGN.md section 6.2: the policy runs
    twice). The row is seeded directly, standing in for SQL that reached APPROVED some way
    other than the worker's draft path.
    """
    drop_column = "ALTER TABLE public.customers DROP COLUMN full_name"
    migration_id = seed_migration(conn, status="APPROVED", sql=drop_column)

    outcome = make_worker("w1").run_once()

    assert outcome == worker.FAILED
    assert row_of(conn, migration_id)["error"].startswith("policy: ")
    assert columns_of(target, "customers") == ORIGINAL_CUSTOMER_COLUMNS
    assert markers_of(target) == {}


def test_existing_marker_with_matching_hash_skips_ddl_and_succeeds(conn, target, make_worker):
    """Guarantee: when a marker with the approved hash already exists, the worker does not
    run the DDL and marks the job SUCCEEDED.

    Invariant: 4. This is the recovery half of "marker first": the marker proves an earlier
    attempt committed. How we know the DDL was skipped: the test inserts only the marker,
    not the column, and the column is still absent afterwards. (Had the DDL run, the column
    would now exist.)
    """
    migration_id = seed_migration(conn, status="APPROVED", sql=ADD_NICKNAME)
    insert_marker(target, migration_id, sha256_hex(ADD_NICKNAME))

    outcome = make_worker("w1").run_once()

    assert outcome == worker.SUCCEEDED
    assert row_of(conn, migration_id)["status"] == "SUCCEEDED"
    assert "nickname" not in columns_of(target, "customers")
    assert markers_of(target) == {migration_id: sha256_hex(ADD_NICKNAME)}


def test_existing_marker_with_different_hash_fails(conn, target, make_worker):
    """Guarantee: an existing marker whose hash is NOT the approved hash never counts as
    success: no DDL runs and the job is marked FAILED.

    Invariant: 4 ("an existing marker counts as success only if its sql_hash matches").
    A marker with another hash means some other SQL was applied under this migration's id.
    """
    migration_id = seed_migration(conn, status="APPROVED", sql=ADD_NICKNAME)
    other_hash = sha256_hex("ALTER TABLE public.customers ADD COLUMN something_else text")
    insert_marker(target, migration_id, other_hash)

    outcome = make_worker("w1").run_once()

    assert outcome == worker.FAILED
    assert row_of(conn, migration_id)["error"] == "target marker exists with a different hash"
    assert "nickname" not in columns_of(target, "customers")
    assert markers_of(target) == {migration_id: other_hash}


def test_lost_target_connection_does_not_mark_failed(conn, target, make_worker):
    """Guarantee: if the target connection dies mid-transaction, the worker writes nothing to
    Relay: the job stays EXECUTING under its lease (to be retried after expiry), and the
    target keeps neither marker nor column.

    Invariant: 4, and DESIGN.md section 1 #8: never mark FAILED on an unknown outcome. From
    the worker's side a dead connection could mean "rolled back" or "committed, answer lost";
    only the next attempt's marker insert can tell. How the loss is forced: a hook that fires
    inside the target transaction, right after the marker insert, has the admin connection
    terminate relay_migrator's session (`pg_terminate_backend`, the server-side `kill`).
    """
    migration_id = seed_migration(conn, status="APPROVED", sql=ADD_NICKNAME)

    def kill_the_target_session():
        killed = target.execute(
            "SELECT pg_terminate_backend(pid) AS done FROM pg_stat_activity WHERE usename = 'relay_migrator'"
        ).fetchall()
        assert len(killed) == 1

    hooks.register("execute.after_marker_insert", kill_the_target_session)
    w1 = make_worker("w1")

    outcome = w1.run_once()

    assert outcome == worker.ABANDONED
    row = row_of(conn, migration_id)
    assert (row["status"], row["error"], row["lease_token"] is not None) == ("EXECUTING", None, True)
    assert markers_of(target) == {}
    assert "nickname" not in columns_of(target, "customers")

    # And the retry, once the lease has expired, starts clean and succeeds.
    hooks.clear()
    expire_lease(conn, migration_id)
    assert w1.run_once() == worker.SUCCEEDED
    assert markers_of(target) == {migration_id: sha256_hex(ADD_NICKNAME)}


def test_no_relay_transaction_is_open_during_target_work(conn, settings, make_worker):
    """Guarantee: while the target transaction is running, the worker's Relay connection has
    no transaction open, and the claim is already visible to everyone as EXECUTING.

    Invariant: 2 (never hold a Relay transaction or row lock across slow work), and the rule
    that no transaction spans both databases. Checked at an exact point: a hook inside the
    target transaction, after the marker insert.
    """
    migration_id = seed_migration(conn, status="APPROVED", sql=ADD_NICKNAME)
    w1 = make_worker("w1")
    seen = {}

    def look():
        seen["relay_txn"] = w1.conn.info.transaction_status
        seen["status"] = conn.execute("SELECT status FROM migrations WHERE id = %s", (migration_id,)).fetchone()["status"]

    hooks.register("execute.after_marker_insert", look)

    assert w1.run_once() == worker.SUCCEEDED
    assert seen == {"relay_txn": psycopg.pq.TransactionStatus.IDLE, "status": "EXECUTING"}


def test_expired_lease_before_target_work_leaves_target_untouched(conn, target, make_worker):
    """Guarantee: a worker whose lease has already expired when it is about to start the
    target transaction stops there: nothing is written to the target or to Relay, and the
    job is recovered the normal way.

    Invariant: narrows the late-apply gap (DESIGN.md section 1 #5, decisions D23 and D33).
    An expired lease means another worker may take the job at any moment, so starting target
    work now would be the stale-worker case. The lease is expired at an exact point (a hook
    right after the claim commits), not by waiting.
    """
    migration_id = seed_migration(conn, status="APPROVED", sql=ADD_NICKNAME)
    expire_once = []

    def expire_first_claim_only():
        if not expire_once:
            expire_once.append(True)
            expire_lease(conn, migration_id)

    hooks.register("claim.after_commit", expire_first_claim_only)
    w1 = make_worker("w1")

    outcome = w1.run_once()

    assert outcome == worker.LOST
    assert row_of(conn, migration_id)["status"] == "EXECUTING"
    assert markers_of(target) == {}
    assert "nickname" not in columns_of(target, "customers")

    assert w1.run_once() == worker.SUCCEEDED     # reclaim, claim again, apply
    assert row_of(conn, migration_id)["attempts"] == 2


def test_lease_with_too_little_time_left_stops_before_target_work(conn, target, make_worker):
    """Guarantee: a worker whose lease is still live, but has less time left than the target
    transaction is allowed to take, does not start: the pass ends as `lost`, the target is
    untouched, and nothing is written to Relay. The job is recovered the normal way.

    Invariant: narrows the late-apply gap (decisions D23, D33). With the defaults the target
    work may take up to 5 s (lock) + 15 s (statement) + 10 s (margin) = 30 s. A lease with
    20 s left could expire while the DDL is still running, letting a second worker in. The
    remaining time is set at an exact point on the database clock (a hook right after the
    claim commits), not by waiting.
    """
    migration_id = seed_migration(conn, status="APPROVED", sql=ADD_NICKNAME)
    shorten_once = []

    def leave_20_seconds_on_the_first_claim():
        if not shorten_once:
            shorten_once.append(True)
            set_lease_remaining(conn, migration_id, 20)

    hooks.register("claim.after_commit", leave_20_seconds_on_the_first_claim)
    w1 = make_worker("w1")

    outcome = w1.run_once()

    assert outcome == worker.LOST
    row = row_of(conn, migration_id)
    assert (row["status"], row["error"], row["lease_token"] is not None) == ("EXECUTING", None, True)
    assert markers_of(target) == {}
    assert "nickname" not in columns_of(target, "customers")

    assert w1.run_once() == worker.IDLE          # the 20 s lease is still live: not reclaimable yet
    expire_lease(conn, migration_id)
    assert w1.run_once() == worker.SUCCEEDED     # reclaimed, claimed with a full lease, applied


def test_lease_with_just_enough_time_left_proceeds(conn, target, make_worker):
    """Guarantee: the rule is "more than the budget", nothing stricter: with 35 s left against
    a 30 s budget the worker goes ahead and the migration succeeds.

    Invariant: none of the six; pins the other side of the D33 boundary so the check cannot
    quietly become "always stop".
    """
    migration_id = seed_migration(conn, status="APPROVED", sql=ADD_NICKNAME)
    hooks.register("claim.after_commit", lambda: set_lease_remaining(conn, migration_id, 35))

    outcome = make_worker("w1").run_once()

    assert outcome == worker.SUCCEEDED
    assert markers_of(target) == {migration_id: sha256_hex(ADD_NICKNAME)}


def test_owner_can_mark_succeeded_after_expiry_if_not_yet_reclaimed(conn, target, make_worker):
    """Guarantee: if the lease expires AFTER the target commit but nobody has reclaimed the
    job yet, the owner's SUCCEEDED write still goes through.

    Invariant: 3 and decision D6. A write checks token and status, not the expiry time: until
    someone reclaims the row, the token on it is still the owner's. The lease is expired by a
    hook between the target commit and the Relay write.
    """
    migration_id = seed_migration(conn, status="APPROVED", sql=ADD_NICKNAME)
    hooks.register("execute.after_target_commit", lambda: expire_lease(conn, migration_id))

    outcome = make_worker("w1").run_once()

    assert outcome == worker.SUCCEEDED
    assert row_of(conn, migration_id)["status"] == "SUCCEEDED"


def test_timeouts_apply_inside_the_target_transaction_only(migrator, settings):
    """Guarantee: lock_timeout, statement_timeout, and idle_in_transaction_session_timeout
    are in force while the target transaction runs and are gone once it ends.

    Invariant: supports 4 and the lease design: the timeouts bound how long the target
    transaction can take, which is what lets a lease outlast it (decision D33).
    `set_config(..., true)` scopes each setting to the current transaction.
    """
    names = ["lock_timeout", "statement_timeout", "idle_in_transaction_session_timeout"]

    def read_settings():
        return [migrator.execute("SELECT current_setting(%s) AS v", (name,)).fetchone()["v"] for name in names]

    inside = {}
    hooks.register("execute.after_marker_insert", lambda: inside.update(values=read_settings()))

    result = target_ops.apply_on_target(
        migrator, uuid.uuid4(), ADD_NICKNAME, sha256_hex(ADD_NICKNAME), settings
    )

    assert result == target_ops.APPLIED
    assert inside["values"] == [settings.lock_timeout, settings.statement_timeout, settings.target_idle_timeout]
    assert read_settings() == ["0", "0", "0"]


def test_apply_on_target_raises_ddl_failed_and_connection_stays_usable(migrator, target, settings):
    """Guarantee: a DDL statement the server rejects surfaces as `DDLFailed` carrying the
    SQLSTATE, the transaction is rolled back (no marker), and the connection is left idle
    and usable.

    Invariant: 4. `DDLFailed` is raised only when the rollback is confirmed; that is what
    lets the worker safely conclude "nothing was applied" and mark the job FAILED.
    """
    bad_sql = "ALTER TABLE public.customers ADD COLUMN email text"

    with pytest.raises(target_ops.DDLFailed) as caught:
        target_ops.apply_on_target(migrator, uuid.uuid4(), bad_sql, sha256_hex(bad_sql), settings)

    assert caught.value.sqlstate == "42701"
    assert markers_of(target) == {}
    assert migrator.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
    assert migrator.execute("SELECT 1 AS ok").fetchone() == {"ok": 1}
