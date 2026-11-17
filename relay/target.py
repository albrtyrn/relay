"""The marker-first target transaction (core operation 3 of 4; DESIGN.md section 4.3).

This is the only code that changes the target database. Two entry points:
- `apply_on_target`: the core operation. Marker, then DDL, in one transaction.
- `probe_marker`: the retry-limit check. The same marker insert, then a rollback; it changes
  nothing, but it waits for an attempt in flight the way `apply_on_target` does.

Words used here:
- DDL ("data definition language"): a statement that changes the shape of the database, such
  as ALTER TABLE ... ADD COLUMN. In Postgres, DDL can be rolled back like any other statement.
- Marker: one row in the target's `relay_applied` table that says "the DDL of this migration
  has been applied here", with the hash of the SQL that was applied.
"""

import uuid

import psycopg
from psycopg.pq import TransactionStatus

from relay import hooks
from relay.config import Settings

# What `apply_on_target` returns.
APPLIED = "applied"                  # this call inserted the marker and ran the DDL
ALREADY_APPLIED = "already_applied"  # a marker with the approved hash was there; no DDL was run
HASH_MISMATCH = "hash_mismatch"      # a marker with a DIFFERENT hash was there; no DDL was run
# What `probe_marker` returns: ALREADY_APPLIED, HASH_MISMATCH, or
NOT_APPLIED = "not_applied"          # no marker, and no attempt in flight; the probe left nothing behind


class DDLFailed(Exception):
    """The target rejected the DDL statement, and the rollback is confirmed: nothing of this
    attempt remains on the target (no schema change, no marker).

    `sqlstate` is Postgres's five-character error code, for example 42701 "duplicate column".
    """

    def __init__(self, sqlstate: str, message: str):
        super().__init__(f"{sqlstate}: {message}")
        self.sqlstate = sqlstate
        self.message = message


def apply_on_target(
    target_conn: psycopg.Connection,
    migration_id: uuid.UUID,
    approved_sql: str,
    approved_hash: str,
    settings: Settings,
) -> str:
    """Apply one approved migration to the target at most once.

    Precondition:
        - The caller holds a committed EXECUTING claim on the migration and has checked that
          sha256(approved_sql) == approved_hash and that the SQL passes the policy.
        - `target_conn` is an autocommit connection to the target as relay_migrator, with no
          transaction open, at READ COMMITTED.
        - The caller has NO Relay transaction open (invariant 2; no transaction spans both
          databases).

    The atomic operation, ONE transaction on the target:
        1. Set lock_timeout, statement_timeout, and idle_in_transaction_session_timeout for
           this transaction only.
        2. Marker step:
               INSERT INTO relay_applied ... ON CONFLICT (migration_id) DO NOTHING RETURNING
           A row came back    -> this transaction now owns the right to apply. Go to 3a.
           No row came back   -> a COMMITTED marker already exists. Go to 3b.
           If another transaction holds an UNcommitted marker for the same id, the INSERT
           waits for it: commit there means "no row" here; rollback there means "row" here.
        3a. DDL step: run the approved SQL exactly as stored.            Result: APPLIED
        3b. Read the existing marker's hash. Equal to approved_hash ->   Result: ALREADY_APPLIED
            Different ->                                                 Result: HASH_MISMATCH
        4. COMMIT. Marker and DDL become durable together, or neither does.

    The marker is inserted BEFORE the DDL so that the insert is both the check ("has this
    been applied?") and the reservation ("I am applying it") in one atomic statement. A
    separate SELECT-then-INSERT would let two workers both see "not applied".

    How it fails:
        - The server rejects the DDL statement (a SQLSTATE came back, including lock_timeout
          and statement_timeout during the DDL) AND the connection is healthy AND the
          rollback is confirmed: raises DDLFailed. The marker rolled back with the DDL.
          The caller marks the job FAILED.
        - Anything else propagates as the original psycopg error and means OUTCOME UNKNOWN:
          an error on the marker step (lock_timeout while waiting on another worker's marker
          is contention, not a failed migration), a lost connection at any point, or an
          error during COMMIT. The caller must write nothing and let its lease expire; the
          next attempt's marker step shows what really happened.
        - Crash at any point before COMMIT finishes: Postgres rolls back marker and DDL
          together. Crash after COMMIT: marker and DDL are both there, and the next attempt
          returns ALREADY_APPLIED.

    Invariant protected:
        4: on the target, the marker is inserted before the DDL in the same transaction, and
           an existing marker counts as success only if its sql_hash matches the approved hash.

    Does not guarantee: that the caller still owns the job when this commits. A worker
    frozen before this call can apply the DDL after Relay has given up on it (the late-apply
    gap, DESIGN.md section 1 #5 and decision D23). The lease re-check in the worker and the
    idle timeout set here narrow that gap; they do not close it.
    """
    step = "begin"
    try:
        with target_conn.transaction():
            _set_transaction_timeouts(target_conn, settings)

            step = "marker"
            inserted = _insert_marker(target_conn, migration_id, approved_hash)
            hooks.fire("execute.after_marker_insert")

            if inserted:
                step = "ddl"
                # No parameters: the SQL is sent to the server byte for byte as approved.
                target_conn.execute(approved_sql)
                result = APPLIED
            else:
                step = "read_marker"
                result = _judge_existing_marker(target_conn, migration_id, approved_hash)

            step = "commit"
        # Leaving the `with` block sent COMMIT and got its acknowledgement.
    except psycopg.Error as error:
        if step == "ddl" and error.sqlstate is not None and _rolled_back_cleanly(target_conn):
            message = error.diag.message_primary or str(error)
            raise DDLFailed(error.sqlstate, message) from error
        raise
    return result


def _rolled_back_cleanly(target_conn: psycopg.Connection) -> bool:
    """True if the connection is alive and idle, which means the ROLLBACK that psycopg sent
    when the error left the `with` block was acknowledged by the server.

    If the connection is broken instead, the server may or may not have finished the
    transaction; that is an unknown outcome, never a DDL failure.
    """
    return (
        not target_conn.closed
        and not target_conn.broken
        and target_conn.info.transaction_status == TransactionStatus.IDLE
    )


def probe_marker(
    target_conn: psycopg.Connection,
    migration_id: uuid.UUID,
    approved_hash: str,
    settings: Settings,
) -> str:
    """Find out whether this migration is applied on the target, WITHOUT applying it, and
    without reaching a verdict while another attempt is still undecided.

    Used when a job is over its retry limit (decisions D8 and D39): the worker must choose
    between SUCCEEDED and FAILED, and an earlier attempt may have committed, or may be inside
    its target transaction right now.

    One transaction on the target, which never runs the DDL and never commits a marker:
        1. Set the same three timeouts as `apply_on_target`.
        2. The same marker INSERT as `apply_on_target` (`_insert_marker`).
           If another transaction holds an UNcommitted marker for this id, the INSERT waits
           for it, exactly as the normal path does.
        3a. A row came back  -> no committed marker exists and no attempt is in flight.
                                ROLL BACK, so this probe leaves no marker.   Result: NOT_APPLIED
        3b. No row came back -> a committed marker exists. Read its hash.
                                Result: ALREADY_APPLIED or HASH_MISMATCH

    Why an INSERT that is thrown away, and not a SELECT: a SELECT cannot see another
    transaction's uncommitted row and does not wait for it. It would answer "no marker"
    while an earlier attempt is one COMMIT away from applying the migration, and the job
    would be marked FAILED and then applied (review finding F1). Only a write to the same
    primary key waits.

    How it fails: any psycopg error propagates and means NO VERDICT. That includes
    lock_timeout while waiting for an in-flight attempt, and a lost connection. The caller
    must write nothing and let its lease expire; the next claim probes again. A probe that
    dies after its insert leaves nothing behind, because it never sends COMMIT.

    Does not guarantee: that no attempt will START after this returns NOT_APPLIED. A stale
    worker frozen before its own marker insert can still apply later (the late-apply gap,
    decision D23). The probe only refuses to decide while an attempt is already in flight.
    """
    with target_conn.transaction():
        _set_transaction_timeouts(target_conn, settings)
        if _insert_marker(target_conn, migration_id, approved_hash):
            # Leaving the block with psycopg.Rollback rolls the transaction back and raises
            # nothing further: the marker inserted one line above is gone again.
            raise psycopg.Rollback
        return _judge_existing_marker(target_conn, migration_id, approved_hash)
    return NOT_APPLIED


# ---------- shared steps of the two target transactions ----------


def _set_transaction_timeouts(target_conn: psycopg.Connection, settings: Settings) -> None:
    """Limit this transaction only: how long a statement may wait for a lock, how long it
    may run, and how long the session may sit idle inside the transaction.

    set_config(name, value, true) is SET LOCAL: it lasts until this transaction ends. Plain
    SET cannot take a bound parameter.
    """
    target_conn.execute("SELECT set_config('lock_timeout', %s, true)", (settings.lock_timeout,))
    target_conn.execute("SELECT set_config('statement_timeout', %s, true)", (settings.statement_timeout,))
    target_conn.execute(
        "SELECT set_config('idle_in_transaction_session_timeout', %s, true)",
        (settings.target_idle_timeout,),
    )


def _insert_marker(target_conn: psycopg.Connection, migration_id: uuid.UUID, approved_hash: str) -> bool:
    """The marker step, in the caller's open target transaction. The ONE place the marker
    INSERT is written, so the normal path and the retry-limit probe cannot drift apart.

    True  -> this transaction inserted the marker (it is still uncommitted).
    False -> a COMMITTED marker for this migration already exists; nothing was inserted.
    If another transaction holds an uncommitted marker for the same id, this waits for it
    (up to lock_timeout): commit there means False here, rollback there means True here.
    """
    inserted = target_conn.execute(
        """
        INSERT INTO relay_applied (migration_id, sql_hash)
        VALUES (%(id)s, %(approved_hash)s)
        ON CONFLICT (migration_id) DO NOTHING
        RETURNING migration_id
        """,
        {"id": migration_id, "approved_hash": approved_hash},
    ).fetchone()
    return inserted is not None


def _judge_existing_marker(target_conn: psycopg.Connection, migration_id: uuid.UUID, approved_hash: str) -> str:
    """After `_insert_marker` returned False: read the committed marker and compare hashes.
    An existing marker counts as success only if its sql_hash is the approved one."""
    existing = target_conn.execute(
        "SELECT sql_hash FROM relay_applied WHERE migration_id = %s", (migration_id,)
    ).fetchone()
    if existing is None:
        # The insert conflicted with a marker that is now gone. relay_migrator cannot
        # delete markers, so only an administrator could cause this.
        raise RuntimeError(f"marker for {migration_id} conflicted and then was not found")
    return ALREADY_APPLIED if existing["sql_hash"] == approved_hash else HASH_MISMATCH
