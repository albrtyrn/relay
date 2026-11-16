"""The REST API: routes, request and response models, error mapping (DESIGN.md section 2).

M1 has create, get, list, and health. Approve and reject arrive in M2.
"""

import uuid
from collections.abc import Iterator
from datetime import datetime
from typing import Annotated, Literal

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StringConstraints

from relay import db, repo
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
    """One connection per request, closed afterwards. No pool (decision D16)."""
    conn = db.connect(request.app.state.settings.relay_db_dsn)
    try:
        yield conn
    finally:
        conn.close()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app. Tests pass Settings pointing at relay_test; uvicorn calls this with none."""
    app = FastAPI(title="Relay")
    app.state.settings = settings if settings is not None else load_settings()

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

    return app
