"""The worker's draft path, one worker at a time (DESIGN.md section 5, M3).

PENDING -> DRAFTING -> (LLM, then policy) -> WAITING_FOR_APPROVAL or FAILED.
"""

from psycopg.pq import TransactionStatus

from relay import db, worker
from relay.llm.fake import FakeLLM
from relay.sqlhash import sha256_hex
from tests.helpers import FixedLLM, FlakyLLM, expire_lease, seed_migration


def row_of(conn, migration_id) -> dict:
    return conn.execute(
        """
        SELECT status, proposed_sql, proposed_sql_hash, risk_level, risk_reason,
               lease_token, lease_expires_at, attempts, error
        FROM migrations WHERE id = %s
        """,
        (migration_id,),
    ).fetchone()


def events_of(conn, migration_id) -> list[tuple]:
    rows = conn.execute(
        "SELECT from_status, to_status, actor FROM migration_events WHERE migration_id = %s ORDER BY id",
        (migration_id,),
    ).fetchall()
    return [(row["from_status"], row["to_status"], row["actor"]) for row in rows]


def test_happy_path_reaches_waiting_with_sql_hash_and_risk(conn, make_worker):
    """Guarantee: one `run_once` takes a PENDING job to WAITING_FOR_APPROVAL with the
    proposed SQL, its sha256 hash, the risk fields, no lease left, and attempts reset to 0.

    Invariant: 1 and 3. Two transitions, each with its event; the save is guarded by the
    worker's own lease token.
    """
    migration_id = seed_migration(conn, status="PENDING", description="add column nickname text to customers")

    outcome = make_worker("w1").run_once()

    expected_sql = "ALTER TABLE public.customers ADD COLUMN nickname text"
    assert outcome == worker.SAVED
    assert row_of(conn, migration_id) == {
        "status": "WAITING_FOR_APPROVAL",
        "proposed_sql": expected_sql,
        "proposed_sql_hash": sha256_hex(expected_sql),
        "risk_level": "LOW",
        "risk_reason": "adds a nullable column; no table rewrite",
        "lease_token": None,
        "lease_expires_at": None,
        "attempts": 0,
        "error": None,
    }
    assert events_of(conn, migration_id) == [
        ("PENDING", "DRAFTING", "worker:w1"),
        ("DRAFTING", "WAITING_FOR_APPROVAL", "worker:w1"),
    ]


def test_idle_when_queue_is_empty(make_worker):
    """Guarantee: with nothing queued, `run_once` reports IDLE (the loop then sleeps).

    Invariant: none of the six.
    """
    assert make_worker("w1").run_once() == worker.IDLE


def test_policy_violation_marks_failed_with_error(conn, make_worker):
    """Guarantee: SQL the policy rejects is never stored as a proposal; the job goes to
    FAILED with `error = 'policy: <rule>'`, on the row and on the audit event.

    Invariant: 3 (the FAILED write is guarded by the worker's token). Policy violations
    happen in the worker, never as an API 422 (DESIGN.md section 1 #11).
    """
    migration_id = seed_migration(conn, status="PENDING", description="add column x text to secrets")

    outcome = make_worker("w1").run_once()

    row = row_of(conn, migration_id)
    assert outcome == worker.FAILED
    assert row["status"] == "FAILED"
    assert row["error"].startswith("policy: ")
    assert row["proposed_sql"] is None
    assert row["lease_token"] is None
    detail = conn.execute(
        "SELECT detail FROM migration_events WHERE migration_id = %s AND to_status = 'FAILED'", (migration_id,)
    ).fetchone()["detail"]
    assert detail == row["error"]


def test_llm_permanent_error_marks_failed(conn, make_worker):
    """Guarantee: an LLM error that retrying cannot fix sends the job to FAILED at once.

    Invariant: 3. No retry budget is spent on a hopeless job.
    """
    migration_id = seed_migration(conn, status="PENDING", description="fake:permanent")

    outcome = make_worker("w1").run_once()

    row = row_of(conn, migration_id)
    assert outcome == worker.FAILED
    assert (row["status"], row["lease_token"]) == ("FAILED", None)
    assert row["error"].startswith("llm: ")


def test_invalid_risk_level_marks_failed(conn, make_worker):
    """Guarantee: an LLM answer with a risk level outside LOW/MEDIUM/HIGH is treated as a
    permanent LLM error: FAILED, nothing stored as a proposal.

    Invariant: none of the six. LLM output is untrusted input (DESIGN.md section 1 #14);
    without this check the row's CHECK constraint would crash the worker instead.
    """
    migration_id = seed_migration(conn, status="PENDING")
    llm = FixedLLM("ALTER TABLE public.customers ADD COLUMN a text", risk_level="EXTREME")

    outcome = make_worker("w1", llm=llm).run_once()

    assert outcome == worker.FAILED
    assert row_of(conn, migration_id)["status"] == "FAILED"


def test_llm_transient_error_leaves_lease_then_retry_succeeds(conn, make_worker):
    """Guarantee: on a temporary LLM error the worker writes nothing and keeps its lease (it
    "abandons" the job); once the lease expires the job is reclaimed and the next attempt
    succeeds.

    Invariant: 3, and the one retry mechanism (decision D7): there is no special "release"
    write, so recovery from an error and recovery from a crash are the same path.
    """
    migration_id = seed_migration(conn, status="PENDING", description="add column nickname text to customers")
    llm = FlakyLLM(FakeLLM(), failures=1)
    w1 = make_worker("w1", llm=llm)

    first = w1.run_once()

    after_first = row_of(conn, migration_id)
    assert first == worker.ABANDONED
    assert after_first["status"] == "DRAFTING"
    assert after_first["lease_token"] is not None
    assert w1.run_once() == worker.IDLE  # the lease has not expired: nothing to reclaim or claim

    expire_lease(conn, migration_id)
    second = w1.run_once()

    assert second == worker.SAVED
    assert row_of(conn, migration_id)["status"] == "WAITING_FOR_APPROVAL"
    assert events_of(conn, migration_id) == [
        ("PENDING", "DRAFTING", "worker:w1"),
        ("DRAFTING", "PENDING", "reclaimer:w1"),
        ("PENDING", "DRAFTING", "worker:w1"),
        ("DRAFTING", "WAITING_FOR_APPROVAL", "worker:w1"),
    ]


def test_claim_is_committed_before_llm_runs(conn, settings, make_worker):
    """Guarantee: while the LLM call is running, the claim is already committed (another
    connection sees DRAFTING) and the worker's own connection has no transaction open.

    Invariant: 2 (never hold a transaction or row lock across an LLM call). The check runs
    from inside the LLM call itself, which is exactly the moment the invariant is about.
    """
    migration_id = seed_migration(conn, status="PENDING", description="add column nickname text to customers")
    seen = {}

    class ObservingLLM:
        def propose(self, description, schema_summary):
            with db.connect(settings.relay_db_dsn) as other:
                seen["status"] = other.execute(
                    "SELECT status FROM migrations WHERE id = %s", (migration_id,)
                ).fetchone()["status"]
            seen["worker_transaction"] = w1.conn.info.transaction_status
            return FakeLLM().propose(description, schema_summary)

    w1 = make_worker("w1", llm=ObservingLLM())

    assert w1.run_once() == worker.SAVED
    assert seen == {"status": "DRAFTING", "worker_transaction": TransactionStatus.IDLE}
