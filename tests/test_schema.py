"""The CHECK and FOREIGN KEY constraints are a second line of defense (DESIGN.md section 3.1).

These tests talk to Postgres directly, with no application code in between, to show that the
database itself refuses bad state even if a code bug tried to write it.
"""

import uuid

import psycopg
import pytest

from tests.helpers import seed_migration


def test_unknown_status_rejected_by_check(conn):
    """Guarantee: a status outside the eight state names cannot be stored.

    Invariant: supports "use exactly these state names" (CLAUDE.md, Architecture) by making the
    database enforce the list.
    """
    with pytest.raises(psycopg.errors.CheckViolation) as error:
        conn.execute("INSERT INTO migrations (description, status) VALUES ('x', 'BOGUS')")

    assert error.value.diag.constraint_name == "status_valid"


def test_lease_on_pending_row_rejected_by_check(conn):
    """Guarantee: a lease can exist only while status is DRAFTING or EXECUTING.

    Invariant: supports 3 (lease checks). A helper bug that leaves a lease on a queued row
    fails loudly here instead of corrupting state.
    """
    with pytest.raises(psycopg.errors.CheckViolation) as error:
        conn.execute(
            """
            INSERT INTO migrations (description, status, lease_token, lease_expires_at)
            VALUES ('x', 'PENDING', gen_random_uuid(), now())
            """
        )

    assert error.value.diag.constraint_name == "lease_matches_status"


def test_leased_status_without_lease_rejected_by_check(conn):
    """Guarantee: the reverse also holds; DRAFTING without a lease token cannot be stored.

    Invariant: supports 3. A worker-owned state always has an owner token to check against.
    """
    with pytest.raises(psycopg.errors.CheckViolation) as error:
        conn.execute("INSERT INTO migrations (description, status) VALUES ('x', 'DRAFTING')")

    assert error.value.diag.constraint_name == "lease_matches_status"


def test_event_requires_existing_migration(conn):
    """Guarantee: an audit event cannot point at a migration that does not exist.

    Invariant: supports the audit trail behind invariants 1 and 3.
    """
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        conn.execute(
            """
            INSERT INTO migration_events (migration_id, from_status, to_status, actor)
            VALUES (%s, NULL, 'PENDING', 'user:test')
            """,
            (uuid.uuid4(),),
        )


def test_seeded_rows_in_every_status_satisfy_the_checks(conn):
    """Guarantee: a legal row exists for every one of the eight states.

    Invariant: none of the six. This guards the test helper itself: later milestones seed rows in
    every state, and a helper that drifts from the schema would make those tests meaningless.
    """
    statuses = ["PENDING", "DRAFTING", "WAITING_FOR_APPROVAL", "APPROVED",
                "EXECUTING", "SUCCEEDED", "REJECTED", "FAILED"]

    for status in statuses:
        seed_migration(conn, status=status)

    stored = conn.execute("SELECT status FROM migrations ORDER BY status").fetchall()
    assert sorted(row["status"] for row in stored) == sorted(statuses)
