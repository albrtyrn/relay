"""The marker-first target transaction (core operation 3 of 4; DESIGN.md section 4.3).

This is the only code that changes the target database.

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
            # set_config(name, value, true) is SET LOCAL: it lasts until this transaction
            # ends. Plain SET cannot take a bound parameter.
            target_conn.execute("SELECT set_config('lock_timeout', %s, true)", (settings.lock_timeout,))
            target_conn.execute("SELECT set_config('statement_timeout', %s, true)", (settings.statement_timeout,))
            target_conn.execute(
                "SELECT set_config('idle_in_transaction_session_timeout', %s, true)",
                (settings.target_idle_timeout,),
            )

            step = "marker"
            inserted = target_conn.execute(
                """
                INSERT INTO relay_applied (migration_id, sql_hash)
                VALUES (%(id)s, %(approved_hash)s)
                ON CONFLICT (migration_id) DO NOTHING
                RETURNING migration_id
                """,
                {"id": migration_id, "approved_hash": approved_hash},
            ).fetchone()
            hooks.fire("execute.after_marker_insert")

            if inserted is not None:
                step = "ddl"
                # No parameters: the SQL is sent to the server byte for byte as approved.
                target_conn.execute(approved_sql)
                result = APPLIED
            else:
                step = "read_marker"
                existing = target_conn.execute(
                    "SELECT sql_hash FROM relay_applied WHERE migration_id = %s", (migration_id,)
                ).fetchone()
                if existing is None:
                    # The insert conflicted with a marker that is now gone. relay_migrator
                    # cannot delete markers, so only an administrator could cause this.
                    raise RuntimeError(f"marker for {migration_id} conflicted and then was not found")
                result = ALREADY_APPLIED if existing["sql_hash"] == approved_hash else HASH_MISMATCH

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


def read_marker_hash(target_conn: psycopg.Connection, migration_id: uuid.UUID) -> str | None:
    """Read-only: the sql_hash of this migration's marker, or None if there is no marker.

    Used when a job is over its retry limit (decision D8): the worker looks before deciding,
    so a migration that was in fact applied is recorded as SUCCEEDED, not FAILED.
    """
    row = target_conn.execute(
        "SELECT sql_hash FROM relay_applied WHERE migration_id = %s", (migration_id,)
    ).fetchone()
    return None if row is None else row["sql_hash"]
