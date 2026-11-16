"""The claim query (core operation 2 of 4) and the reclaim sweep (DESIGN.md sections 4.2, 4.5).

Neither function here contains an UPDATE of `status`. Both pick and lock rows with
`SELECT ... FOR UPDATE SKIP LOCKED`, then change them through the transition helper, in the
same transaction (decision D3). That keeps invariant 1 literally true.
"""

import uuid
from dataclasses import dataclass

import psycopg

from relay.transitions import transition


@dataclass(frozen=True)
class Claim:
    """What a worker holds after a successful claim. `lease_token` is its proof of ownership:
    every later write to this row must present it (invariant 3)."""

    id: uuid.UUID
    status: str            # the state the row is now in: DRAFTING (EXECUTING from M4)
    lease_token: uuid.UUID
    attempts: int          # claims in the current phase, this one included
    description: str


def claim_one(conn: psycopg.Connection, worker_id: str, lease_seconds: int) -> Claim | None:
    """Take ownership of the oldest queued job, or return None if there is none to take.

    Precondition:
        An autocommit connection with no transaction open. The function opens one short
        transaction and returns only after it has COMMITTED.

    The atomic operation, in one transaction:
        1. SELECT the oldest PENDING row FOR UPDATE SKIP LOCKED.
           FOR UPDATE locks the chosen row until commit. SKIP LOCKED makes other claimers
           pass over a locked row instead of waiting for it, so two workers never queue for,
           or take, the same job.
        2. transition(PENDING -> DRAFTING), which in the same UPDATE sets a fresh
           lease_token, a lease_expires_at computed on the DATABASE clock, and attempts + 1,
           and writes the audit event.
        3. COMMIT.

    How it fails:
        - Nothing queued, or every candidate is locked by another claimer: returns None.
        - Crash before COMMIT: the lock is released and the row is still PENDING.
        - Crash after COMMIT: the row is DRAFTING with a live lease and a dead owner. The
          reclaim sweep below returns it to the queue once the lease expires.
        - The helper's UPDATE cannot match 0 rows here, because this transaction holds the
          row lock. If it ever does, that is a bug, so it raises.

    Invariant protected:
        2: the claim is committed before any LLM work, so no lock is held across slow work.
        3: the fresh token is the only proof of ownership for the worker's later writes.

    Does not guarantee: strict oldest-first order under contention, or that a job is
    processed only once. SKIP LOCKED only prevents two SIMULTANEOUS claims; the lease and the
    token check cover everything after the commit.

    M3 claims only PENDING rows. M4 widens the WHERE to APPROVED when the execute path exists.
    """
    with conn.transaction():
        row = conn.execute(
            """
            SELECT id, attempts, description,
                   now() + make_interval(secs => %(lease_seconds)s) AS lease_expires_at
            FROM migrations
            WHERE status = 'PENDING'
            ORDER BY created_at
            LIMIT 1
            FOR UPDATE SKIP LOCKED
            """,
            {"lease_seconds": lease_seconds},
        ).fetchone()
        if row is None:
            return None

        claim = Claim(
            id=row["id"],
            status="DRAFTING",
            lease_token=uuid.uuid4(),
            attempts=row["attempts"] + 1,
            description=row["description"],
        )
        changed = transition(
            conn, claim.id, "PENDING", "DRAFTING", f"worker:{worker_id}",
            set_fields={
                "lease_token": claim.lease_token,
                "lease_expires_at": row["lease_expires_at"],
                "attempts": claim.attempts,
            },
        )
        if not changed:
            raise RuntimeError(f"claim of {claim.id} matched 0 rows while holding its row lock")
    # The `with` block has ended: the claim is committed. Only now does the caller get it.
    return claim


def reclaim_expired(conn: psycopg.Connection, worker_id: str, batch_size: int = 10) -> int:
    """Return expired DRAFTING jobs to PENDING. Returns how many were reclaimed.

    Every worker runs this at the top of each loop; there is no separate reaper process.

    One transaction: SELECT up to `batch_size` DRAFTING rows whose lease_expires_at is in the
    past (database clock) FOR UPDATE SKIP LOCKED, then for each one
    transition(DRAFTING -> PENDING) guarded by the expired token just read.

    - Why the token guard is right here: the row is locked and the token was read under that
      lock, so the guard says "I am ending exactly this expired lease" (DESIGN section 1 #4).
    - Race with the slow owner: its final write and this reclaim update the same row.
      Whichever commits first wins; the owner then matches 0 rows, or this sweep skips the
      row the owner has locked. Either way there is one consistent outcome.
    - `attempts` is left alone, so the retry limit keeps counting across reclaims.
    - Reclaim never judges the earlier attempt: the draft is simply redone.
    - Crash mid-sweep: the transaction rolls back and the next loop repeats it.

    M3 reclaims only DRAFTING. M4 adds EXECUTING -> APPROVED.
    """
    with conn.transaction():
        rows = conn.execute(
            """
            SELECT id, lease_token
            FROM migrations
            WHERE status = 'DRAFTING'
              AND lease_expires_at < now()
            ORDER BY lease_expires_at
            LIMIT %(batch_size)s
            FOR UPDATE SKIP LOCKED
            """,
            {"batch_size": batch_size},
        ).fetchall()
        for row in rows:
            changed = transition(
                conn, row["id"], "DRAFTING", "PENDING", f"reclaimer:{worker_id}",
                lease_token=row["lease_token"],
                set_fields={"lease_token": None, "lease_expires_at": None},
            )
            if not changed:
                raise RuntimeError(f"reclaim of {row['id']} matched 0 rows while holding its row lock")
    return len(rows)
