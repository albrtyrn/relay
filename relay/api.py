"""The REST API: routes, request and response models, error mapping (DESIGN.md section 2).

M1: create, get, list, health. M2: approve and reject, bound to the SQL hash.
"""

import uuid
from collections.abc import Iterator
from datetime import datetime
from typing import Annotated, Literal

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StringConstraints

from relay import db, repo, service
from relay.config import Settings, load_settings

# The eight state names, exactly as in CLAUDE.md. The database CHECK constraint enforces the
# same list; repeating it here lets FastAPI answer 422 for `?status=BOGUS`.
Status = Literal[
    "PENDING", "DRAFTING", "WAITING_FOR_APPROVAL", "APPROVED",
    "EXECUTING", "SUCCEEDED", "REJECTED", "FAILED",
]


# ---------- request and response models ----------


class CreateMigrationRequest(BaseModel):
    """Only a description. v1 never accepts SQL from a client: the SQL comes from the LLM."""

    # `extra="forbid"` turns an unexpected field (say, "proposed_sql") into a 422 instead of
    # silently ignoring it.
    model_config = ConfigDict(extra="forbid")

    # Same 1..2000 limit as the database CHECK. Stripping first means "   " counts as empty.
    description: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


# A sha256 in lower-case hex, the same shape the database CHECK requires.
SqlHash = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class ApproveRequest(BaseModel):
    """The hash of the SQL the reviewer saw. Never the SQL itself: v1 has no editing."""

    model_config = ConfigDict(extra="forbid")

    sql_hash: SqlHash


class RejectRequest(BaseModel):
    """Reject is bound to the hash too, and needs a reason (decision D29)."""

    model_config = ConfigDict(extra="forbid")

    sql_hash: SqlHash
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


class MigrationOut(BaseModel):
    """A migration as the API shows it. There is no lease_token or lease_expires_at (D25)."""

    id: uuid.UUID
    description: str
    status: Status
    proposed_sql: str | None
    proposed_sql_hash: str | None
    approved_sql_hash: str | None
    risk_level: str | None
    risk_reason: str | None
    attempts: int
    error: str | None
    created_at: datetime
    updated_at: datetime


class EventOut(BaseModel):
    id: int
    from_status: str | None
    to_status: str
    actor: str
    detail: str | None
    created_at: datetime


class MigrationDetailOut(MigrationOut):
    events: list[EventOut]


# ---------- app ----------


def get_conn(request: Request) -> Iterator[psycopg.Connection]:
    """One connection per request, closed afterwards. No pool (decision D16). Opened with
    the Relay session timeouts (decision D40)."""
    conn = db.connect_relay(request.app.state.settings)
    try:
        yield conn
    finally:
        conn.close()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app. Tests pass Settings pointing at relay_test; uvicorn calls this with none."""
    app = FastAPI(title="Relay")
    app.state.settings = settings if settings is not None else load_settings()

    @app.exception_handler(psycopg.errors.LockNotAvailable)
    def row_is_busy(_request: Request, _error: psycopg.errors.LockNotAvailable):
        """503 when a statement waited longer than RELAY_LOCK_TIMEOUT for a row lock
        (decision D40; DESIGN.md section 1 #26).

        This happens when another request on the same migration has changed the row and not
        yet committed (or its process froze before committing). By the time this runs, the
        handler's `with conn.transaction():` block has already rolled back, so this request
        changed nothing. Not 409: the other request has not committed and may still roll
        back, so "someone else decided" is not known. 503 with Retry-After says what is
        known: busy, nothing happened, try again. The driver's error text is not returned.
        """
        return JSONResponse(
            status_code=503,
            content={"detail": "migration is busy: another request is changing it; nothing was changed, retry"},
            headers={"Retry-After": "1"},
        )

    @app.get("/health")
    def health(request: Request):
        """200 when relay_db answers SELECT 1, else 503 (DESIGN.md section 1 #19)."""
        try:
            with db.connect(request.app.state.settings.relay_db_dsn) as conn:
                conn.execute("SELECT 1")
        except psycopg.Error:
            return JSONResponse(status_code=503, content={"status": "unavailable"})
        return {"status": "ok"}

    @app.post("/migrations", status_code=202, response_model=MigrationOut)
    def create_migration(
        body: CreateMigrationRequest,
        # Required on every POST (DESIGN.md section 1 #15). A label, not authentication.
        # Up to 195 characters so "user:" + the name fits the 200-character actor column.
        x_user: Annotated[str, Header(min_length=1, max_length=195)],
        conn: Annotated[psycopg.Connection, Depends(get_conn)],
    ):
        """202 Accepted: the row is PENDING and a worker will draft the SQL later."""
        return repo.create_migration(conn, body.description, actor=f"user:{x_user}")

    @app.get("/migrations/{migration_id}", response_model=MigrationDetailOut)
    def get_migration(
        migration_id: uuid.UUID,
        conn: Annotated[psycopg.Connection, Depends(get_conn)],
    ):
        migration = repo.get_migration(conn, migration_id)
        if migration is None:
            raise HTTPException(status_code=404, detail="migration not found")
        return {**migration, "events": repo.get_events(conn, migration_id)}

    @app.get("/migrations", response_model=list[MigrationOut])
    def list_migrations(
        conn: Annotated[psycopg.Connection, Depends(get_conn)],
        status: Status | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ):
        return repo.list_migrations(conn, status, limit)

    # What approve and reject can answer besides 200, for the generated API docs.
    decision_responses = {
        404: {"description": "Unknown migration id"},
        409: {"description": "Not WAITING_FOR_APPROVAL, or sql_hash is not the current proposal's"},
        503: {"description": "Another request is changing this migration; nothing was changed. Retry (see Retry-After)"},
    }

    @app.post("/migrations/{migration_id}/approve", response_model=MigrationOut, responses=decision_responses)
    def approve_migration(
        migration_id: uuid.UUID,
        body: ApproveRequest,
        x_user: Annotated[str, Header(min_length=1, max_length=195)],
        conn: Annotated[psycopg.Connection, Depends(get_conn)],
        # Accepted and ignored until M5, which makes it required (DESIGN.md section 1 #1).
        idempotency_key: Annotated[str | None, Header()] = None,
    ):
        """Approve the proposal exactly as generated. 409 for the wrong state or a stale hash."""
        # One transaction per request, opened here and not in the service.
        # M5 SEAM: the idempotency reservation (insert the key, run the service call, save its
        # answer) replaces this block and uses the same single transaction.
        with conn.transaction():
            status_code, result = service.approve(conn, migration_id, body.sql_hash, actor=f"user:{x_user}")
        return JSONResponse(status_code=status_code, content=jsonable_encoder(result))

    @app.post("/migrations/{migration_id}/reject", response_model=MigrationOut, responses=decision_responses)
    def reject_migration(
        migration_id: uuid.UUID,
        body: RejectRequest,
        x_user: Annotated[str, Header(min_length=1, max_length=195)],
        conn: Annotated[psycopg.Connection, Depends(get_conn)],
        idempotency_key: Annotated[str | None, Header()] = None,  # ignored until M5, as above
    ):
        """Reject the proposal with a reason. Same rules as approve."""
        with conn.transaction():  # M5 SEAM: same as approve
            status_code, result = service.reject(
                conn, migration_id, body.sql_hash, body.reason, actor=f"user:{x_user}"
            )
        return JSONResponse(status_code=status_code, content=jsonable_encoder(result))

    return app
