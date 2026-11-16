"""What the relay_migrator role can and cannot do on the target (DESIGN.md section 3.2, M4).

The worker runs approved SQL on the target as `relay_migrator`, a login with as little power
as the job allows ("least privilege"). If a bad statement ever got past the policy, this role
is the second fence: it cannot create tables, cannot rewrite the record of what was applied,
and cannot reach Relay's own state.
"""

import uuid

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from relay import db
from tests.conftest import RELAY_TEST_DB
from tests.helpers import SEED_HASH, columns_of


def test_migrator_is_not_superuser(migrator):
    """Guarantee: the worker's target login is relay_migrator, and that role has none of the
    cluster-wide powers (superuser, create database, create role, replication).

    Invariant: none of the six; least privilege on the target (PLAN). A superuser would
    bypass every grant the tests below rely on.
    """
    row = migrator.execute(
        """
        SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolreplication
        FROM pg_roles WHERE rolname = current_user
        """
    ).fetchone()

    assert row == {
        "rolname": "relay_migrator", "rolsuper": False, "rolcreatedb": False,
        "rolcreaterole": False, "rolreplication": False,
    }


@pytest.mark.parametrize("table", ["customers", "orders"])
def test_migrator_can_alter_demo_tables(migrator, target, table):
    """Guarantee: the role can do the one thing approved migrations do: add a column to a
    demo table.

    Invariant: none of the six. Postgres has no "may ALTER" grant; only the table's owner
    can, which is why db/target_schema.sql makes relay_migrator the owner of the demo tables.
    """
    migrator.execute(f"ALTER TABLE public.{table} ADD COLUMN added_by_migrator text")

    assert "added_by_migrator" in columns_of(target, table)


def test_migrator_cannot_create_table(migrator):
    """Guarantee: the role cannot create new tables in the target's public schema.

    Invariant: none of the six; least privilege. The policy only allows ADD COLUMN anyway;
    this shows the database itself would refuse more.
    """
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        migrator.execute("CREATE TABLE public.sneaky (id int)")


def test_migrator_cannot_update_or_delete_markers(migrator, target):
    """Guarantee: the role can add and read markers but can never change or remove one.

    Invariant: 4. The marker is the proof that a migration's DDL was applied. If the role
    that runs the DDL could delete or edit markers, a bug (or bad SQL) could erase that
    proof and the migration would be applied a second time.
    """
    migration_id = uuid.uuid4()
    migrator.execute("INSERT INTO relay_applied (migration_id, sql_hash) VALUES (%s, %s)", (migration_id, SEED_HASH))

    for statement in [
        "UPDATE relay_applied SET sql_hash = repeat('0', 64)",
        "DELETE FROM relay_applied",
        "TRUNCATE relay_applied",
        "DROP TABLE relay_applied",
    ]:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            migrator.execute(statement)

    rows = target.execute("SELECT migration_id, sql_hash FROM relay_applied").fetchall()
    assert rows == [{"migration_id": migration_id, "sql_hash": SEED_HASH}]


def test_migrator_cannot_connect_to_relay_db(settings):
    """Guarantee: the target role cannot even open a connection to Relay's own database.

    Invariant: supports 1 and 3. Workflow state, leases, and the audit log live in relay_db;
    the login that runs AI-written SQL must have no path to them. dbinit revokes CONNECT on
    the Relay database from PUBLIC (every role), and relay_migrator has no grant of its own.
    """
    relay_dsn_as_migrator = make_conninfo(settings.target_db_dsn, dbname=RELAY_TEST_DB)

    with pytest.raises(psycopg.OperationalError, match="permission denied for database"):
        db.connect(relay_dsn_as_migrator)
