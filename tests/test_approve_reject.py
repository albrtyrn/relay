"""The approve and reject endpoints over HTTP, one request at a time (DESIGN.md section 8, M2).

Workers do not exist yet, so each test seeds its row directly in the state it needs
(DESIGN.md section 1 #22). The many-at-once case lives in test_concurrent_approval.py.
"""

import uuid

import pytest

from relay.sqlhash import sha256_hex
from tests.helpers import SEED_HASH, seed_migration

USER = {"X-User": "alice"}
WAITING = "WAITING_FOR_APPROVAL"
# A well-formed hash of some other SQL: what a reviewer would send after looking at an old proposal.
STALE_HASH = sha256_hex("ALTER TABLE public.customers ADD COLUMN something_else text")


def approve(client, migration_id, sql_hash=SEED_HASH, headers=USER):
    return client.post(f"/migrations/{migration_id}/approve", json={"sql_hash": sql_hash}, headers=headers)


def reject(client, migration_id, sql_hash=SEED_HASH, reason="wrong table", headers=USER):
    return client.post(
        f"/migrations/{migration_id}/reject",
        json={"sql_hash": sql_hash, "reason": reason},
        headers=headers,
    )


def stored(conn, migration_id) -> dict:
    return conn.execute(
        "SELECT status, approved_sql_hash FROM migrations WHERE id = %s", (migration_id,)
    ).fetchone()


def events_of(conn, migration_id) -> list[dict]:
    return conn.execute(
        """
        SELECT from_status, to_status, actor, detail
        FROM migration_events WHERE migration_id = %s ORDER BY id
        """,
        (migration_id,),
    ).fetchall()


def test_approve_200_sets_approved_hash_and_event(client, conn):
    """Guarantee: approving a waiting proposal with the hash the reviewer saw answers 200,
    stores APPROVED with that hash, and writes one audit event naming the reviewer.

    Invariant: 1 and 3 (the change goes through the transition helper, together with its event).
    """
    migration_id = seed_migration(conn, status=WAITING)

    response = approve(client, migration_id)

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(migration_id)
    assert body["status"] == "APPROVED"
    assert body["approved_sql_hash"] == SEED_HASH
    assert "lease_token" not in body
    assert stored(conn, migration_id) == {"status": "APPROVED", "approved_sql_hash": SEED_HASH}
    assert events_of(conn, migration_id) == [
        {"from_status": WAITING, "to_status": "APPROVED", "actor": "user:alice", "detail": None}
    ]


def test_approve_stale_hash_409_and_state_unchanged(client, conn):
    """Guarantee: approving with a hash that is not the stored proposal's hash answers 409
    and changes nothing.

    Invariant: approval is bound to the exact SQL ("a human chose this exact statement",
    PLAN.md). A reviewer cannot approve SQL they did not see.
    """
    migration_id = seed_migration(conn, status=WAITING)

    response = approve(client, migration_id, sql_hash=STALE_HASH)

    assert response.status_code == 409
    assert "detail" in response.json()
    assert stored(conn, migration_id) == {"status": WAITING, "approved_sql_hash": None}
    assert events_of(conn, migration_id) == []


@pytest.mark.parametrize("action", [approve, reject], ids=["approve", "reject"])
def test_unknown_id_404(client, conn, action):
    """Guarantee: approve and reject on a well-formed id that does not exist answer 404 and
    write nothing.

    Invariant: none of the six; error mapping. (DESIGN names this test_approve_unknown_id_404;
    it covers reject too.)
    """
    response = action(client, uuid.uuid4())

    assert response.status_code == 404
    assert conn.execute("SELECT count(*) AS n FROM migration_events").fetchone()["n"] == 0


@pytest.mark.parametrize(
    "status",
    ["PENDING", "DRAFTING", "APPROVED", "EXECUTING", "SUCCEEDED", "REJECTED", "FAILED"],
)
@pytest.mark.parametrize("action", [approve, reject], ids=["approve", "reject"])
def test_decision_when_not_waiting_409(client, conn, action, status):
    """Guarantee: approve and reject answer 409 from each of the seven states that are not
    WAITING_FOR_APPROVAL, and the row keeps its status.

    Invariant: 1. Only WAITING_FOR_APPROVAL has an edge to APPROVED or REJECTED; an illegal
    move is a conflict with the current state (409), not a server error. (DESIGN names this
    test_approve_when_not_waiting_409; it covers reject too.)
    """
    migration_id = seed_migration(conn, status=status)

    response = action(client, migration_id)

    assert response.status_code == 409
    assert stored(conn, migration_id)["status"] == status
    assert events_of(conn, migration_id) == []


def test_reject_200_records_reason_in_event(client, conn):
    """Guarantee: rejecting a waiting proposal answers 200, stores REJECTED, and keeps the
    reason on the audit event.

    Invariant: 1 and 3. The reason lives in migration_events.detail (decision D21).
    """
    migration_id = seed_migration(conn, status=WAITING)

    response = reject(client, migration_id, reason="  this drops nothing but is the wrong table  ")

    assert response.status_code == 200
    assert response.json()["status"] == "REJECTED"
    assert stored(conn, migration_id) == {"status": "REJECTED", "approved_sql_hash": None}
    assert events_of(conn, migration_id) == [
        {
            "from_status": WAITING,
            "to_status": "REJECTED",
            "actor": "user:alice",
            "detail": "this drops nothing but is the wrong table",
        }
    ]


def test_reject_stale_hash_409_and_state_unchanged(client, conn):
    """Guarantee: reject is bound to the SQL hash too: a stale hash answers 409 and changes
    nothing.

    Invariant: a decision always refers to the exact SQL the reviewer saw (decision D29).
    """
    migration_id = seed_migration(conn, status=WAITING)

    response = reject(client, migration_id, sql_hash=STALE_HASH)

    assert response.status_code == 409
    assert stored(conn, migration_id)["status"] == WAITING
    assert events_of(conn, migration_id) == []


def test_reject_after_approve_409(client, conn):
    """Guarantee: once a proposal is approved, a later reject answers 409 and the approval
    stands, with only the approval in the audit log.

    Invariant: 1. The first decision wins; the second finds the row no longer waiting.
    """
    migration_id = seed_migration(conn, status=WAITING)
    assert approve(client, migration_id).status_code == 200

    response = reject(client, migration_id)

    assert response.status_code == 409
    assert stored(conn, migration_id)["status"] == "APPROVED"
    assert [event["to_status"] for event in events_of(conn, migration_id)] == ["APPROVED"]


def test_approve_after_reject_409(client, conn):
    """Guarantee: the mirror case: a rejected proposal cannot be approved afterwards.

    Invariant: 1. REJECTED is terminal.
    """
    migration_id = seed_migration(conn, status=WAITING)
    assert reject(client, migration_id).status_code == 200

    response = approve(client, migration_id)

    assert response.status_code == 409
    assert stored(conn, migration_id) == {"status": "REJECTED", "approved_sql_hash": None}


@pytest.mark.parametrize("action", [approve, reject], ids=["approve", "reject"])
def test_missing_x_user_422(client, conn, action):
    """Guarantee: approve and reject without X-User answer 422 and change nothing.

    Invariant: supports 3: every audit event needs an actor (DESIGN.md section 1 #15).
    """
    migration_id = seed_migration(conn, status=WAITING)

    response = action(client, migration_id, headers={})

    assert response.status_code == 422
    assert stored(conn, migration_id)["status"] == WAITING
    assert events_of(conn, migration_id) == []


@pytest.mark.parametrize(
    ("endpoint", "body"),
    [
        ("approve", {}),
        ("approve", {"sql_hash": "abc"}),
        ("approve", {"sql_hash": SEED_HASH.upper()}),
        ("approve", {"sql_hash": SEED_HASH, "proposed_sql": "DROP TABLE customers"}),
        ("reject", {"sql_hash": SEED_HASH}),
        ("reject", {"sql_hash": SEED_HASH, "reason": "   "}),
        ("reject", {"sql_hash": SEED_HASH, "reason": "x" * 2001}),
        ("reject", {"reason": "no hash"}),
    ],
    ids=[
        "approve-missing-hash", "approve-short-hash", "approve-uppercase-hash", "approve-unknown-field",
        "reject-missing-reason", "reject-blank-reason", "reject-reason-too-long", "reject-missing-hash",
    ],
)
def test_bad_body_422(client, conn, endpoint, body):
    """Guarantee: a malformed approve or reject body answers 422 before any database write.

    Invariant: none of the six; input validation at the edge. 422 means "I cannot read this
    request"; 409 means "I read it, and it conflicts with the current state". The
    unknown-field case shows a reviewer cannot slip replacement SQL into an approval.
    """
    migration_id = seed_migration(conn, status=WAITING)

    response = client.post(f"/migrations/{migration_id}/{endpoint}", json=body, headers=USER)

    assert response.status_code == 422
    assert stored(conn, migration_id)["status"] == WAITING
    assert events_of(conn, migration_id) == []


@pytest.mark.parametrize("endpoint", ["approve", "reject"])
def test_malformed_id_422(client, endpoint):
    """Guarantee: an id that is not a UUID answers 422, not 404 or 500.

    Invariant: none of the six.
    """
    response = client.post(
        f"/migrations/not-a-uuid/{endpoint}",
        json={"sql_hash": SEED_HASH, "reason": "x"},
        headers=USER,
    )

    assert response.status_code == 422


def test_idempotency_key_header_is_accepted_and_ignored(client, conn):
    """Guarantee (M2 to M4 only): an Idempotency-Key header is accepted but has no effect yet:
    repeating an approve with the same key answers 409, not a replay of the first 200.

    Invariant: none yet. This pins today's behavior so that M5, which makes the key required
    and replays the saved answer, has to change this test on purpose (DESIGN.md section 1 #1).
    """
    migration_id = seed_migration(conn, status=WAITING)
    headers = {**USER, "Idempotency-Key": "key-1"}

    first = approve(client, migration_id, headers=headers)
    second = approve(client, migration_id, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 409
    assert len(events_of(conn, migration_id)) == 1
