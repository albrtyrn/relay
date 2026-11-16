"""Many decisions on one migration at the same moment (DESIGN.md section 8, M2).

These tests call the service layer from threads, each with its own database connection. They
do not go through HTTP: the race lives in Postgres, and N separate connections exercise it
exactly (decision D13). That is why the README may say "concurrent approvals" but not
"concurrent requests".
"""

from relay import service
from tests.helpers import SEED_HASH, run_behind_barrier, seed_migration

WAITING = "WAITING_FOR_APPROVAL"


def approver(migration_id, name):
    """One reviewer's approve, written the way the route handler writes it: the caller opens
    the transaction, the service works inside it."""
    def action(conn):
        with conn.transaction():
            return service.approve(conn, migration_id, SEED_HASH, actor=f"user:{name}")
    return action


def rejecter(migration_id, name):
    def action(conn):
        with conn.transaction():
            return service.reject(conn, migration_id, SEED_HASH, reason="no", actor=f"user:{name}")
    return action


def events_of(conn, migration_id) -> list[dict]:
    return conn.execute(
        "SELECT from_status, to_status, actor FROM migration_events WHERE migration_id = %s ORDER BY id",
        (migration_id,),
    ).fetchall()


def test_20_concurrent_approvals_exactly_one_wins(conn, settings):
    """Guarantee: of 20 approvals of one migration released at the same moment, exactly 1
    succeeds, 19 get a 409, and there is exactly one approval event, written by the winner.

    Invariant: 1 and 3. The compare-and-set in the transition helper lets one UPDATE match;
    every other one either waits on the row lock and then matches 0 rows, or reads the row
    after the winner committed. Either way the answer is 409 and no event is written.
    The race is forced with a Barrier (all 20 connections are open and waiting before any
    acts), not with sleeps.
    """
    migration_id = seed_migration(conn, status=WAITING)
    names = [f"reviewer-{n}" for n in range(20)]

    results = run_behind_barrier(settings.relay_db_dsn, [approver(migration_id, name) for name in names])

    status_codes = [status_code for status_code, _body in results]
    assert sorted(status_codes) == [200] + [409] * 19
    winner = names[status_codes.index(200)]
    assert events_of(conn, migration_id) == [
        {"from_status": WAITING, "to_status": "APPROVED", "actor": f"user:{winner}"}
    ]
    row = conn.execute("SELECT status, approved_sql_hash FROM migrations WHERE id = %s", (migration_id,)).fetchone()
    assert row == {"status": "APPROVED", "approved_sql_hash": SEED_HASH}


def test_concurrent_approve_and_reject_exactly_one_wins(conn, settings):
    """Guarantee: of 10 approvals and 10 rejections released at the same moment, exactly one
    decision takes effect; the final status and the single audit event both belong to it.

    Invariant: 1 and 3. Approve and reject are two different transitions out of the same
    state, and the same compare-and-set makes them exclude each other: a migration is never
    both approved and rejected.
    """
    migration_id = seed_migration(conn, status=WAITING)
    actions = []
    for n in range(10):
        actions.append(approver(migration_id, f"approver-{n}"))
        actions.append(rejecter(migration_id, f"rejecter-{n}"))

    results = run_behind_barrier(settings.relay_db_dsn, actions)

    status_codes = [status_code for status_code, _body in results]
    assert sorted(status_codes) == [200] + [409] * 19
    winning_body = results[status_codes.index(200)][1]
    assert winning_body["status"] in ("APPROVED", "REJECTED")

    final_status = conn.execute("SELECT status FROM migrations WHERE id = %s", (migration_id,)).fetchone()["status"]
    assert final_status == winning_body["status"]
    events = events_of(conn, migration_id)
    assert len(events) == 1
    assert events[0]["from_status"] == WAITING
    assert events[0]["to_status"] == final_status
