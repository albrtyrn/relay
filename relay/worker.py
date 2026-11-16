"""The worker: claim a job, do the slow work with no transaction open, save the result
(DESIGN.md section 5). Run one with `python -m relay.worker`.

M3 implements the draft path: PENDING -> DRAFTING -> (LLM, policy) -> WAITING_FOR_APPROVAL.
The execute path arrives in M4.

The shape of every job is the same, and it is what keeps invariant 2:

    short transaction: claim, COMMIT
    slow work, with NO transaction open
    short transaction: save, guarded by my lease token, COMMIT

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
from relay.config import SCHEMA_SUMMARY, Settings, load_settings
from relay.llm import RISK_LEVELS, LLMClient, LLMPermanentError, LLMTransientError, get_llm
from relay.sqlhash import sha256_hex
from relay.transitions import transition

log = logging.getLogger("relay.worker")

# What one pass of the loop did. `run_once` returns one of these.
IDLE = "idle"            # nothing to claim
SAVED = "saved"          # a proposal was saved; the job is WAITING_FOR_APPROVAL
FAILED = "failed"        # the job was marked FAILED
ABANDONED = "abandoned"  # temporary problem; nothing written, the lease will expire
LOST = "lost"            # my final write matched 0 rows: the job is someone else's now

CLEAR_LEASE = {"lease_token": None, "lease_expires_at": None}


class Worker:
    def __init__(self, settings: Settings, llm: LLMClient, worker_id: str | None = None):
        self.settings = settings
        self.llm = llm
        self.worker_id = worker_id or settings.worker_id
        # One long-lived autocommit connection. No transaction is open between steps.
        self.conn = db.connect(settings.relay_db_dsn)
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

        if claim.attempts > self.settings.max_attempts:
            # This job has used up its tries in this phase (decision D8). The check is made
            # by the worker that just claimed it, so the FAILED write has a valid token.
            return self._fail(claim, "retry limit reached")
        return self._draft(claim)

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
        # 0 rows: my lease was reclaimed and the job belongs to someone else now.
        # Stop here; never retry the write.
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
                self.conn = db.connect(self.settings.relay_db_dsn)
                return
            except psycopg.OperationalError:
                self._stop.wait(delay)
                delay = min(delay * 2, 30)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = load_settings()
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
