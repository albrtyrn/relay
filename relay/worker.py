"""The worker: claim a job, do the slow work with no transaction open, save the result
(DESIGN.md section 5). Run one with `python -m relay.worker`.

Two paths, chosen by what was claimed:
- draft (M3):    PENDING  -> DRAFTING  -> (LLM, policy)        -> WAITING_FOR_APPROVAL
- execute (M4):  APPROVED -> EXECUTING -> (target transaction) -> SUCCEEDED or FAILED

The shape of every job is the same, and it is what keeps invariant 2:

    short Relay transaction: claim, COMMIT
    slow work, with NO Relay transaction open
    short Relay transaction: save, guarded by my lease token, COMMIT

There is one retry mechanism: abandon the lease (decision D7). A worker that cannot finish
writes nothing more; the lease expires, the reclaim sweep returns the job to the queue, and
`attempts` counts up to the limit. Recovery from an error and from a crash are the same path.
"""

import logging
import random
import signal
import threading

import psycopg

from relay import db, hooks, policy
from relay.claim import Claim, claim_one, reclaim_expired
from relay.config import (
    SCHEMA_SUMMARY,
    Settings,
    check_lease_outlasts_target_work,
    load_settings,
    target_work_budget_seconds,
)
from relay.llm import RISK_LEVELS, LLMClient, LLMPermanentError, LLMTransientError, get_llm
from relay.sqlhash import sha256_hex
from relay.target import ALREADY_APPLIED, HASH_MISMATCH, DDLFailed, apply_on_target, probe_marker
from relay.transitions import transition

log = logging.getLogger("relay.worker")

# What one pass of the loop did. `run_once` returns one of these.
IDLE = "idle"            # nothing to claim
SAVED = "saved"          # a proposal was saved; the job is WAITING_FOR_APPROVAL
SUCCEEDED = "succeeded"  # the migration is applied on the target; the job is SUCCEEDED
FAILED = "failed"        # the job was marked FAILED
ABANDONED = "abandoned"  # temporary problem or unknown outcome; nothing written, the lease will expire
LOST = "lost"            # the job is no longer mine (0 rows on a write, or my lease ran out)

CLEAR_LEASE = {"lease_token": None, "lease_expires_at": None}


class Worker:
    def __init__(self, settings: Settings, llm: LLMClient, worker_id: str | None = None):
        # Refuse to start if a lease could run out while the target transaction is still
        # allowed to run (decision D33). Checked before any connection is opened.
        check_lease_outlasts_target_work(settings)
        self.settings = settings
        self.llm = llm
        self.worker_id = worker_id or settings.worker_id
        # One long-lived autocommit connection. No transaction is open between steps, which
        # is also why its idle-in-transaction timeout (decision D40) never fires during slow
        # work: that timeout only counts time spent INSIDE an open transaction.
        self.conn = db.connect_relay(settings)
        self._stop = threading.Event()

    def close(self) -> None:
        self.conn.close()

    # ---------- one pass ----------

    def run_once(self) -> str:
        """Reclaim expired leases, then claim and process at most one job."""
        reclaim_expired(self.conn, self.worker_id)                                    # R1: Relay txn

        claim = claim_one(self.conn, self.worker_id, self.settings.lease_seconds)     # R2: Relay txn
        if claim is None:
            return IDLE
        # The claim is committed. From here on this worker holds no transaction and no lock.
        hooks.fire("claim.after_commit")

        try:
            if claim.attempts > self.settings.max_attempts:
                # This job has used up its tries in this phase (decision D8). The check is made
                # by the worker that just claimed it, so its final write has a valid token.
                return self._exhausted(claim)
            if claim.status == "DRAFTING":
                return self._draft(claim)
            return self._execute(claim)
        except psycopg.errors.LockNotAvailable as error:
            # One of my writes to Relay waited longer than RELAY_LOCK_TIMEOUT for the row
            # (decision D40): someone holds it in a transaction that has not finished, for
            # example a reclaim of this very job. The write and its audit event were rolled
            # back together, so nothing was changed. Treat it like every temporary problem:
            # write nothing more and let the lease expire (decision D7). The next attempt
            # redoes a draft, or finds the marker if the target had already committed.
            # (A lock timeout on the TARGET never arrives here: _execute and _exhausted
            # handle those themselves.)
            log.warning("job %s: a Relay write timed out waiting for the row, abandoning the lease: %s", claim.id, error)
            return ABANDONED

    # ---------- draft path (PENDING jobs) ----------

    def _draft(self, claim: Claim) -> str:
        # The slow part. No transaction is open here (invariant 2).
        try:
            proposal = self.llm.propose(claim.description, SCHEMA_SUMMARY)
            if proposal.risk_level not in RISK_LEVELS:
                raise LLMPermanentError(f"invalid risk_level {proposal.risk_level!r}")
            # LLM output is untrusted: the policy, not the model, decides what may run.
            policy.validate(proposal.sql)
        except LLMTransientError as error:
            log.warning("job %s: temporary LLM error, abandoning the lease: %s", claim.id, error)
            return ABANDONED
        except LLMPermanentError as error:
            return self._fail(claim, f"llm: {error}")
        except policy.PolicyViolation as error:
            return self._fail(claim, f"policy: {error.rule}")

        hooks.fire("draft.before_save")

        # R3: save, only if the job is still mine (my token) and still DRAFTING.
        with self.conn.transaction():
            saved = transition(
                self.conn, claim.id, "DRAFTING", "WAITING_FOR_APPROVAL", f"worker:{self.worker_id}",
                lease_token=claim.lease_token,
                set_fields={
                    "proposed_sql": proposal.sql,
                    "proposed_sql_hash": sha256_hex(proposal.sql),
                    "risk_level": proposal.risk_level,
                    "risk_reason": proposal.risk_reason,
                    # The retry budget is per phase: execution starts from 0 (decision D30).
                    "attempts": 0,
                    **CLEAR_LEASE,
                },
            )
        return SAVED if saved else self._lost(claim)

    # ---------- execute path (APPROVED jobs) ----------

    def _execute(self, claim: Claim) -> str:
        """Run one approved migration on the target, then tell Relay what happened.

        The steps, and where each transaction begins and ends:

            checks   (no transaction)   stored SQL still has the approved hash; policy again
            connect  (no transaction)   open the target connection as relay_migrator
            re-check (one Relay read)   the job is still mine, with enough lease left for T1
            T1       (target txn)       marker first, then the DDL, COMMIT  -> relay/target.py
            R4       (Relay txn)        EXECUTING -> SUCCEEDED or FAILED, guarded by my token

        T1 and R4 are separate transactions on separate databases; nothing can make them
        commit together. If this worker dies between them, the target is already changed and
        Relay still says EXECUTING. That is safe because of the marker: the next worker's T1
        finds it and reports ALREADY_APPLIED instead of running the DDL again.
        """
        # An approval is for one exact statement. If the stored SQL no longer hashes to what
        # the reviewer approved, it is unapproved SQL: never run it.
        if sha256_hex(claim.proposed_sql) != claim.approved_sql_hash:
            return self._fail(claim, "stored SQL does not match the approved hash")
        # The policy runs a second time, just before execution (DESIGN.md section 6.2).
        try:
            policy.validate(claim.proposed_sql)
        except policy.PolicyViolation as error:
            return self._fail(claim, f"policy: {error.rule}")

        try:
            target_conn = db.connect(self.settings.target_db_dsn)
        except psycopg.Error as error:
            log.warning("job %s: cannot reach the target, abandoning the lease: %s", claim.id, error)
            return ABANDONED
        try:
            # Last look before touching the target. If my lease does not have enough time
            # left to cover the whole target transaction, another worker could take this job
            # while I am still inside it, which would make me the stale worker (DESIGN.md
            # section 1 #5). Stop without writing anything; the lease expires and the job
            # is retried with a fresh one.
            if not self._lease_covers_target_work(claim):
                return self._lost(claim)
            try:
                result = apply_on_target(                                             # T1: target txn
                    target_conn, claim.id, claim.proposed_sql, claim.approved_sql_hash, self.settings
                )
            except DDLFailed as error:
                # The target refused the statement and rolled everything back, marker included.
                return self._fail(claim, f"ddl: {error}")
            except psycopg.Error as error:
                # Outcome unknown (lost connection, marker contention, error during COMMIT).
                # Never mark FAILED on an unknown outcome (DESIGN.md section 1 #8): the next
                # attempt's marker insert will show whether the target committed.
                log.warning("job %s: target outcome unknown, abandoning the lease: %s", claim.id, error)
                return ABANDONED
        finally:
            target_conn.close()

        # The target transaction has committed. Relay does not know yet.
        hooks.fire("execute.after_target_commit")

        if result == HASH_MISMATCH:
            return self._fail(claim, "target marker exists with a different hash")
        # APPLIED (I ran the DDL) or ALREADY_APPLIED (an earlier attempt did): both are success.
        return self._succeed(claim)

    def _lease_covers_target_work(self, claim: Claim) -> bool:
        """One plain read, no lock: is this EXECUTING job still mine, with MORE lease time
        left than the target transaction is allowed to take?

        The time left is measured on the database clock (`now()`), like every other lease
        comparison, and compared with lock_timeout + statement_timeout + the safety margin
        (decision D33). So a worker that was slow to get here does not start work it might
        not finish inside its lease.

        This is stricter than the token check on a write. A write is accepted after expiry
        as long as nobody has reclaimed the row (decision D6), because finishing is harmless.
        STARTING target work without enough lease left is not: it invites a second worker in.
        """
        row = self.conn.execute(
            """
            SELECT 1 AS mine FROM migrations
            WHERE id = %(id)s AND status = 'EXECUTING' AND lease_token = %(token)s
              AND lease_expires_at > now() + make_interval(secs => %(budget)s)
            """,
            {"id": claim.id, "token": claim.lease_token, "budget": target_work_budget_seconds(self.settings)},
        ).fetchone()
        return row is not None

    def _succeed(self, claim: Claim) -> str:
        """R4: mark my job SUCCEEDED, only if it is still mine."""
        with self.conn.transaction():
            succeeded = transition(
                self.conn, claim.id, "EXECUTING", "SUCCEEDED", f"worker:{self.worker_id}",
                lease_token=claim.lease_token,
                set_fields=CLEAR_LEASE,
            )
        if not succeeded:
            # Whoever holds the job now will find the marker and record the success.
            return self._lost(claim)
        log.info("job %s: SUCCEEDED", claim.id)
        return SUCCEEDED

    # ---------- shared endings ----------

    def _exhausted(self, claim: Claim) -> str:
        """The job is over its retry limit for this phase (decision D8).

        A draft has no side effects, so it simply fails. An execution may have committed on
        the target in an earlier attempt that died before telling Relay, or an earlier
        attempt may be inside its target transaction at this moment. So ask the target with
        `probe_marker`, which waits for an attempt in flight instead of deciding around it
        (decision D39):

            T1' (target txn)   marker insert as a question, then ROLLBACK  -> relay/target.py
            R4' (Relay txn)    EXECUTING -> SUCCEEDED or FAILED, guarded by my token

        No Relay transaction is open during T1'. No lease re-check is needed before it
        (unlike T1): the probe never applies anything, so starting it late is harmless, and
        the final write is refused anyway if the job is no longer mine.
        """
        if claim.status == "DRAFTING":
            return self._fail(claim, "retry limit reached")

        try:
            target_conn = db.connect(self.settings.target_db_dsn)
            try:
                verdict = probe_marker(target_conn, claim.id, claim.approved_sql_hash, self.settings)
            finally:
                target_conn.close()
        except psycopg.Error as error:
            # No verdict: the target is unreachable, or the probe timed out waiting for an
            # attempt that is still in flight. Never FAILED on an unknown outcome; the next
            # claim (still over the limit) probes again.
            log.warning("job %s: no verdict from the marker probe, abandoning the lease: %s", claim.id, error)
            return ABANDONED
        if verdict == ALREADY_APPLIED:
            return self._succeed(claim)
        if verdict == HASH_MISMATCH:
            # Not applied as approved, and something else claims this migration id on the
            # target. Say so: the anomaly should not hide behind the retry limit.
            return self._fail(claim, "retry limit reached; target marker exists with a different hash")
        return self._fail(claim, "retry limit reached")

    def _fail(self, claim: Claim, error: str) -> str:
        """Mark my job FAILED, only if it is still mine."""
        with self.conn.transaction():
            failed = transition(
                self.conn, claim.id, claim.status, "FAILED", f"worker:{self.worker_id}",
                lease_token=claim.lease_token,
                detail=error,
                set_fields={"error": error, **CLEAR_LEASE},
            )
        if not failed:
            return self._lost(claim)
        log.info("job %s: FAILED: %s", claim.id, error)
        return FAILED

    def _lost(self, claim: Claim) -> str:
        # The job belongs, or may at any moment belong, to someone else: a write matched
        # 0 rows, or the lease ran out before target work began. Stop here; never retry.
        log.warning("job %s: lost the lease, stopping work on it", claim.id)
        return LOST

    # ---------- the loop ----------

    def run_forever(self) -> None:
        """Loop until asked to stop (SIGTERM or Ctrl-C). The current job is always finished first."""
        log.info("worker %s started", self.worker_id)
        while not self._stop.is_set():
            try:
                outcome = self.run_once()
            except psycopg.OperationalError:
                # Lost the Relay connection. Any claim this worker held is covered by its lease.
                log.exception("lost the database connection; reconnecting")
                self._reconnect()
                continue
            except Exception:
                # A bug. Abandon the lease; the retry limit stops a crash loop.
                log.exception("unexpected error; abandoning the current job")
                outcome = IDLE
            if outcome == IDLE:
                # Sleep a little, with random jitter so workers do not all poll in step.
                # Waiting on the stop event (not time.sleep) lets SIGTERM end the wait at once.
                self._stop.wait(self.settings.poll_seconds * random.uniform(1.0, 1.25))
        log.info("worker %s stopped", self.worker_id)

    def stop(self) -> None:
        self._stop.set()

    def _reconnect(self) -> None:
        delay = self.settings.poll_seconds
        while not self._stop.is_set():
            try:
                self.conn.close()
                self.conn = db.connect_relay(self.settings)
                return
            except psycopg.OperationalError:
                self._stop.wait(delay)
                delay = min(delay * 2, 30)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = load_settings()
    # Worker() raises ValueError here if LEASE_SECONDS is too short for the target timeouts
    # (decision D33); the process exits before claiming anything.
    worker = Worker(settings, get_llm(settings))
    # `docker compose stop` sends SIGTERM: finish the current job, then leave the loop.
    signal.signal(signal.SIGTERM, lambda _signum, _frame: worker.stop())
    signal.signal(signal.SIGINT, lambda _signum, _frame: worker.stop())
    try:
        worker.run_forever()
    finally:
        worker.close()


if __name__ == "__main__":
    main()
