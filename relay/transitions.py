"""The transition helper: core operation 1 of 4 (DESIGN.md section 4.1).

This file holds the ONLY `UPDATE` of `migrations.status` in the codebase. (The one other
write of `status` is the INSERT that creates a row as PENDING, in relay/repo.py.)
"""

import uuid

import psycopg
from psycopg import sql
from psycopg.pq import TransactionStatus

# The eight state names, exactly as in CLAUDE.md.
STATUSES = (
    "PENDING", "DRAFTING", "WAITING_FOR_APPROVAL", "APPROVED",
    "EXECUTING", "SUCCEEDED", "REJECTED", "FAILED",
)

# The whole workflow, as (from, to) pairs. Anything not listed here cannot happen.
# SUCCEEDED, REJECTED, and FAILED never appear on the left: they are terminal.
LEGAL_TRANSITIONS = frozenset({
    ("PENDING", "DRAFTING"),                   # a worker claims the job to draft SQL
    ("DRAFTING", "WAITING_FOR_APPROVAL"),      # the worker saved a proposal
    ("DRAFTING", "FAILED"),                    # drafting failed for good
    ("DRAFTING", "PENDING"),                   # lease expired: back to the queue
    ("WAITING_FOR_APPROVAL", "APPROVED"),      # a human approved
    ("WAITING_FOR_APPROVAL", "REJECTED"),      # a human rejected
    ("APPROVED", "EXECUTING"),                 # a worker claims the job to run it
    ("EXECUTING", "SUCCEEDED"),                # the migration is applied
    ("EXECUTING", "FAILED"),                   # the migration failed for good
    ("EXECUTING", "APPROVED"),                 # lease expired: back to the queue
    # The one edge that keeps the status. A worker that could not even connect to the
    # target gives its attempt back (set_fields = {attempts: one less}) and keeps its lease
    # (decision D41). It is in this table, and not a separate UPDATE elsewhere, so that it
    # gets the same lease-token check and the same audit event as every other worker write.
    ("EXECUTING", "EXECUTING"),
})

# States in which a worker owns the row. A transition FROM one always requires a lease token
# (that includes the same-state edge above).
LEASED_STATUSES = frozenset({"DRAFTING", "EXECUTING"})

# The only columns a caller may set alongside the status. `status` itself is not here, so it
# cannot be changed except through `to_status`.
SETTABLE_COLUMNS = frozenset({
    "proposed_sql", "proposed_sql_hash", "risk_level", "risk_reason", "approved_sql_hash",
    "lease_token", "lease_expires_at", "attempts", "error",
})


def transition(
    conn: psycopg.Connection,
    migration_id: uuid.UUID,
    from_status: str,
    to_status: str,
    actor: str,
    *,
    lease_token: uuid.UUID | None = None,
    detail: str | None = None,
    set_fields: dict | None = None,
) -> bool:
    """Move one migration from `from_status` to `to_status`, if it is still in `from_status`.

    Returns True if the row changed, False if it did not (and then nothing was written).

    Precondition:
        The caller has a transaction open (`with conn.transaction():`) on an autocommit
        connection at the READ COMMITTED isolation level. This function never begins or
        commits; the caller decides what else shares the commit (decision D2). Called with no
        transaction open, it raises RuntimeError (decision D28).

    The atomic operation:
        A compare-and-set, meaning "change the value only if it is still what I expect":
            UPDATE migrations SET status = <to> ... WHERE id = <id> AND status = <from>
                                                    [AND lease_token = <mine>]
        and, only if that UPDATE changed a row, one INSERT into migration_events. Both sit in
        the caller's transaction, so they commit together or not at all.

    How it fails:
        - 0 rows updated: the row is in another status, the lease token is not the caller's,
          or the id does not exist. Returns False and writes no event. Never retried: the API
          answers 409; a worker stops, because the job is no longer its own.
        - Two transactions at once: the second UPDATE waits on the first one's row lock. When
          the first commits, Postgres re-checks the second's WHERE against the new row, the
          status no longer matches, and the second gets 0 rows. If the first rolls back
          instead, the second goes ahead. This re-check is READ COMMITTED behavior; at
          REPEATABLE READ the second would get a serialization error (decision D27).
        - Crash before the caller commits: Postgres rolls back the UPDATE and the INSERT.
        - A caller bug (illegal pair, missing lease token, unknown column): raises ValueError
          before any SQL runs.

    Invariant protected:
        1: every status change goes through this function.
        3: a worker's write checks its lease token and the expected status, and commits
           atomically with its audit event.
    """
    set_fields = dict(set_fields or {})

    # --- Checks before any SQL. A failure here is a bug in the caller, not a conflict. ---
    if (from_status, to_status) not in LEGAL_TRANSITIONS:
        raise ValueError(f"illegal transition {from_status} -> {to_status}")
    if from_status in LEASED_STATUSES and lease_token is None:
        raise ValueError(f"leaving {from_status} requires a lease_token (invariant 3)")
    unknown = sorted(set(set_fields) - SETTABLE_COLUMNS)
    if unknown:
        raise ValueError(f"set_fields may not set {unknown}")
    if conn.info.transaction_status != TransactionStatus.INTRANS:
        raise RuntimeError(
            "transition() needs the caller's open transaction: without one, the status change "
            "and its audit event would commit separately"
        )

    # --- Build the UPDATE. Values are always parameters. Column names cannot be parameters,
    # so they are quoted with sql.Identifier, and only names from SETTABLE_COLUMNS get here. ---
    params = {"id": migration_id, "from_status": from_status, "to_status": to_status}
    assignments = [sql.SQL("status = %(to_status)s"), sql.SQL("updated_at = now()")]
    for column, value in set_fields.items():
        assignments.append(
            sql.SQL("{} = {}").format(sql.Identifier(column), sql.Placeholder(f"set_{column}"))
        )
        params[f"set_{column}"] = value

    conditions = [sql.SQL("id = %(id)s"), sql.SQL("status = %(from_status)s")]
    if lease_token is not None:
        conditions.append(sql.SQL("lease_token = %(lease_token)s"))
        params["lease_token"] = lease_token

    update = sql.SQL("UPDATE migrations SET {} WHERE {} RETURNING id").format(
        sql.SQL(", ").join(assignments),
        sql.SQL(" AND ").join(conditions),
    )

    # --- The compare-and-set. ---
    if conn.execute(update, params).fetchone() is None:
        return False

    # --- The audit event, in the same transaction, only because the UPDATE changed a row. ---
    conn.execute(
        """
        INSERT INTO migration_events (migration_id, from_status, to_status, actor, detail)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (migration_id, from_status, to_status, actor, detail),
    )
    return True
