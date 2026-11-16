"""Plain SQL for creating and reading migrations (DESIGN.md section 2). No HTTP in this file.

Every function takes an open connection and returns plain dicts.
"""

import uuid

import psycopg

# The columns the API may show. lease_token and lease_expires_at are left out on purpose:
# the token is a worker's proof of ownership and must never leave Relay (decision D25).
# This string is a fixed constant, never built from user input, so it is safe inside the SQL.
PUBLIC_COLUMNS = """id, description, status, proposed_sql, proposed_sql_hash, approved_sql_hash,
       risk_level, risk_reason, attempts, error, created_at, updated_at"""


def create_migration(conn: psycopg.Connection, description: str, actor: str) -> dict:
    """Insert a PENDING migration and its creation event in one transaction.

    Precondition: an autocommit connection with no transaction open (see relay/db.py).
    Atomic operation: the INSERT into migrations and the INSERT into migration_events commit
        together or not at all.
    How it fails: if either INSERT errors, or the process dies before COMMIT, Postgres rolls
        both back. There is never a migration without its first audit event, or the reverse.
    Invariant: 1. Creation is the one allowed write of `status` that does not go through the
        transition helper (DESIGN.md section 1 #3): the row is born PENDING, so there is no
        earlier status to compare against. Every later change goes through the helper (M2).
    """
    with conn.transaction():
        row = conn.execute(
            f"INSERT INTO migrations (description) VALUES (%s) RETURNING {PUBLIC_COLUMNS}",
            (description,),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO migration_events (migration_id, from_status, to_status, actor)
            VALUES (%s, NULL, 'PENDING', %s)
            """,
            (row["id"], actor),
        )
    return row


def get_migration(conn: psycopg.Connection, migration_id: uuid.UUID) -> dict | None:
    return conn.execute(
        f"SELECT {PUBLIC_COLUMNS} FROM migrations WHERE id = %s",
        (migration_id,),
    ).fetchone()


def get_events(conn: psycopg.Connection, migration_id: uuid.UUID) -> list[dict]:
    return conn.execute(
        """
        SELECT id, from_status, to_status, actor, detail, created_at
        FROM migration_events
        WHERE migration_id = %s
        ORDER BY id
        """,
        (migration_id,),
    ).fetchall()


def list_migrations(conn: psycopg.Connection, status: str | None, limit: int) -> list[dict]:
    """Newest first. `status=None` means every status. The id breaks ties between equal times."""
    return conn.execute(
        f"""
        SELECT {PUBLIC_COLUMNS}
        FROM migrations
        WHERE (%(status)s::text IS NULL OR status = %(status)s::text)
        ORDER BY created_at DESC, id DESC
        LIMIT %(limit)s
        """,
        {"status": status, "limit": limit},
    ).fetchall()
