"""Approve and reject decisions (DESIGN.md section 2). No HTTP in this file.

Each function returns `(status_code, body)` instead of raising for 404 and 409. That is the
seam for M5: the idempotency layer will save exactly this pair and replay it for a retried
request (DESIGN.md section 4.4).

Like the transition helper, these functions run inside the CALLER's transaction and never
begin or commit one. Today the route handler opens it; in M5 the idempotency reservation
opens it, so the key, the status change, the audit event, and the saved answer share one commit.
"""

import uuid

import psycopg

from relay import repo
from relay.transitions import transition

WAITING = "WAITING_FOR_APPROVAL"


def approve(conn: psycopg.Connection, migration_id: uuid.UUID, sql_hash: str, actor: str) -> tuple[int, dict]:
    """Approve the proposal whose hash the reviewer saw. 200, 404, or 409."""
    return _decide(
        conn, migration_id, sql_hash, actor,
        to_status="APPROVED",
        detail=None,
        # Record which exact SQL was approved. The worker re-checks this hash before running it.
        set_fields={"approved_sql_hash": sql_hash},
    )


def reject(conn: psycopg.Connection, migration_id: uuid.UUID, sql_hash: str, reason: str, actor: str) -> tuple[int, dict]:
    """Reject the proposal whose hash the reviewer saw; the reason goes on the audit event."""
    return _decide(
        conn, migration_id, sql_hash, actor,
        to_status="REJECTED",
        detail=reason,
        set_fields=None,
    )


def _decide(
    conn: psycopg.Connection,
    migration_id: uuid.UUID,
    sql_hash: str,
    actor: str,
    *,
    to_status: str,
    detail: str | None,
    set_fields: dict | None,
) -> tuple[int, dict]:
    """The shared flow: read, check, compare-and-set (decision D27).

    Step 1 is a plain read with no lock. Its only job is to choose the right error message.
    Step 2, the transition helper's `WHERE status = 'WAITING_FOR_APPROVAL'`, is what makes the
    decision safe when several arrive at once.

    Why a lock-free read is enough: a row never comes back to WAITING_FOR_APPROVAL after
    leaving it, and nothing edits the proposal while it waits. So if step 2 still finds the
    row waiting, the hash checked in step 1 is still the row's hash.
    """
    # Step 1: read.
    row = conn.execute(
        "SELECT status, proposed_sql_hash FROM migrations WHERE id = %s",
        (migration_id,),
    ).fetchone()
    if row is None:
        return 404, {"detail": "migration not found"}
    if row["status"] != WAITING:
        return 409, {"detail": f"migration is {row['status']}, not {WAITING}"}
    if row["proposed_sql_hash"] != sql_hash:
        return 409, {"detail": "sql_hash does not match the current proposal"}

    # Step 2: the compare-and-set. False means another decision committed between step 1 and
    # here; this request lost the race.
    changed = transition(conn, migration_id, WAITING, to_status, actor, detail=detail, set_fields=set_fields)
    if not changed:
        return 409, {"detail": f"migration is no longer {WAITING}"}

    # Step 3: the row as it now stands. A transaction sees its own uncommitted writes.
    return 200, repo.get_migration(conn, migration_id)
