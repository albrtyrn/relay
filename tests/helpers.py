"""Shared test helpers. Grows with the milestones (DESIGN.md section 2)."""

import uuid

from relay.sqlhash import sha256_hex

# What each status requires to exist in the row, mirroring the CHECK constraints in
# db/relay_schema.sql. seed_migration fills these in so a seeded row is a legal row.
_NEEDS_PROPOSAL = {"WAITING_FOR_APPROVAL", "APPROVED", "EXECUTING", "SUCCEEDED", "REJECTED"}
_NEEDS_APPROVAL = {"APPROVED", "EXECUTING", "SUCCEEDED"}
_NEEDS_LEASE = {"DRAFTING", "EXECUTING"}

SEED_SQL = "ALTER TABLE public.customers ADD COLUMN seeded text"


def seed_migration(conn, status: str = "PENDING", description: str = "seeded row") -> uuid.UUID:
    """Insert a row directly in any status and return its id.

    This is test setup, not application behavior: it bypasses the API and writes no audit
    event. Tests may INSERT a row in any state this way but never UPDATE status
    (DESIGN.md section 7.2).
    """
    has_proposal = status in _NEEDS_PROPOSAL
    has_approval = status in _NEEDS_APPROVAL
    has_lease = status in _NEEDS_LEASE
    sql_hash = sha256_hex(SEED_SQL)
    row = conn.execute(
        """
        INSERT INTO migrations (description, status, proposed_sql, proposed_sql_hash,
                                approved_sql_hash, lease_token, lease_expires_at)
        VALUES (%(description)s, %(status)s, %(sql)s, %(proposed_hash)s, %(approved_hash)s,
                %(token)s, %(expires)s)
        RETURNING id
        """,
        {
            "description": description,
            "status": status,
            "sql": SEED_SQL if has_proposal else None,
            "proposed_hash": sql_hash if has_proposal else None,
            "approved_hash": sql_hash if has_approval else None,
            "token": uuid.uuid4() if has_lease else None,
            # A far-future timestamp: nothing in M1 looks at it, and M3 tests backdate it.
            "expires": "2999-01-01T00:00:00Z" if has_lease else None,
        },
    ).fetchone()
    return row["id"]
