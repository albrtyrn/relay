"""The create, get, and list endpoints (DESIGN.md section 8, M1)."""

import uuid

import pytest

from tests.helpers import seed_migration

USER = {"X-User": "alice"}


def create(client, description="add column nickname text to customers", headers=USER):
    return client.post("/migrations", json={"description": description}, headers=headers)


def test_create_returns_202_and_row_is_pending(client, conn):
    """Guarantee: creating a migration answers 202 Accepted and stores a PENDING row.

    Invariant: 1 (the initial INSERT with status PENDING is the one allowed write that does
    not go through the transition helper, DESIGN.md section 1 #3). 202 because the real work
    (drafting) happens later in a worker.
    """
    response = create(client)

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "PENDING"
    assert body["description"] == "add column nickname text to customers"
    stored = conn.execute("SELECT status, attempts FROM migrations WHERE id = %s", (body["id"],)).fetchone()
    assert stored == {"status": "PENDING", "attempts": 0}


def test_create_writes_one_creation_event(client, conn):
    """Guarantee: creation leaves exactly one audit event, from NULL to PENDING, by the caller.

    Invariant: supports the audit log (DESIGN.md section 1 #17). The row and its event are
    written in one transaction, so there is never a migration without its first event.
    """
    migration_id = create(client).json()["id"]

    events = conn.execute(
        "SELECT from_status, to_status, actor FROM migration_events WHERE migration_id = %s",
        (migration_id,),
    ).fetchall()
    assert events == [{"from_status": None, "to_status": "PENDING", "actor": "user:alice"}]


def test_create_empty_description_422(client, conn):
    """Guarantee: an empty description is rejected with 422 and nothing is stored.

    Invariant: none of the six; input validation at the edge (DESIGN.md section 1 #11).
    """
    response = create(client, description="")

    assert response.status_code == 422
    assert conn.execute("SELECT count(*) AS n FROM migrations").fetchone()["n"] == 0


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"description": "   "},
        {"description": 123},
        {"description": "x" * 2001},
        {"description": "ok", "proposed_sql": "DROP TABLE customers"},
    ],
    ids=["missing", "whitespace-only", "not-a-string", "too-long", "unknown-field"],
)
def test_create_bad_body_422(client, conn, body):
    """Guarantee: a malformed body is rejected with 422 and nothing is stored.

    Invariant: none of the six. The unknown-field case shows v1 cannot accept SQL from a
    client: only a description is allowed, and the SQL always comes from the LLM.
    """
    response = client.post("/migrations", json=body, headers=USER)

    assert response.status_code == 422
    assert conn.execute("SELECT count(*) AS n FROM migrations").fetchone()["n"] == 0


def test_create_missing_x_user_422(client, conn):
    """Guarantee: a POST without X-User is rejected with 422 and nothing is stored.

    Invariant: supports the audit log: every event needs an actor (DESIGN.md section 1 #15).
    X-User is a label, not authentication.
    """
    response = client.post("/migrations", json={"description": "add column a text to customers"})

    assert response.status_code == 422
    assert conn.execute("SELECT count(*) AS n FROM migrations").fetchone()["n"] == 0


def test_get_returns_row_and_events(client):
    """Guarantee: GET returns the stored row plus its audit events, oldest first.

    Invariant: none of the six; this is the read side of the create transaction.
    """
    created = create(client).json()

    response = client.get(f"/migrations/{created['id']}")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == created["id"]
    assert body["status"] == "PENDING"
    assert body["proposed_sql"] is None
    assert len(body["events"]) == 1
    assert body["events"][0]["to_status"] == "PENDING"
    assert body["events"][0]["actor"] == "user:alice"


def test_get_hides_lease_fields(client, conn):
    """Guarantee: the API never returns lease_token or lease_expires_at.

    Invariant: supports 3. The lease token is a worker's proof of ownership; anyone who could
    read it from the API could impersonate the worker. Decision D25.
    """
    migration_id = seed_migration(conn, status="DRAFTING")

    body = client.get(f"/migrations/{migration_id}").json()

    assert body["status"] == "DRAFTING"
    assert "lease_token" not in body
    assert "lease_expires_at" not in body


def test_get_unknown_id_404(client):
    """Guarantee: a well-formed id that does not exist answers 404.

    Invariant: none of the six.
    """
    response = client.get(f"/migrations/{uuid.uuid4()}")

    assert response.status_code == 404


def test_get_malformed_id_422(client):
    """Guarantee: an id that is not a UUID answers 422, before any database query.

    Invariant: none of the six.
    """
    response = client.get("/migrations/not-a-uuid")

    assert response.status_code == 422


def test_list_filters_by_status(client, conn):
    """Guarantee: GET /migrations?status=X returns only rows in status X; no filter returns all.

    Invariant: none of the six.
    """
    pending_id = seed_migration(conn, status="PENDING")
    waiting_id = seed_migration(conn, status="WAITING_FOR_APPROVAL")
    seed_migration(conn, status="FAILED")

    only_pending = client.get("/migrations", params={"status": "PENDING"}).json()
    only_waiting = client.get("/migrations", params={"status": "WAITING_FOR_APPROVAL"}).json()
    everything = client.get("/migrations").json()

    assert [m["id"] for m in only_pending] == [str(pending_id)]
    assert [m["id"] for m in only_waiting] == [str(waiting_id)]
    assert len(everything) == 3


def test_list_unknown_status_422(client):
    """Guarantee: a status filter outside the eight state names answers 422, not an empty list.

    Invariant: none of the six. A typo like `?status=PENDNG` should be loud, not silently empty.
    """
    response = client.get("/migrations", params={"status": "BOGUS"})

    assert response.status_code == 422


def test_list_is_newest_first_and_respects_limit(client):
    """Guarantee: the list is newest first and `limit` caps it (DESIGN.md section 1 #18).

    Invariant: none of the six.
    """
    ids = [create(client, description=f"change {n}").json()["id"] for n in range(3)]

    page = client.get("/migrations", params={"limit": 2}).json()

    assert [m["id"] for m in page] == [ids[2], ids[1]]


@pytest.mark.parametrize("limit", [0, 201, "abc"])
def test_list_bad_limit_422(client, limit):
    """Guarantee: `limit` outside 1..200 or not a number answers 422.

    Invariant: none of the six.
    """
    response = client.get("/migrations", params={"limit": limit})

    assert response.status_code == 422
