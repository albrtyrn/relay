"""Create the databases, the target role, and the schemas (DESIGN.md sections 2, 3.2, 7.2;
decisions D17, D34).

    python -m relay.dbinit            create anything missing, never drop (safe on every `up`)
    python -m relay.dbinit --reset    DROP both databases first, then rebuild (destroys data)

The test suite calls `init_databases` too, with test database names, so tests build their
databases exactly the way the real stack does.
"""

import argparse
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from relay import db
from relay.config import load_admin_dsn, load_migrator_password

RELAY_DB_NAME = "relay_db"
TARGET_DB_NAME = "target_db"
# The login the worker uses on the target. The name is fixed because db/target_schema.sql
# names it in its OWNER TO and GRANT lines.
MIGRATOR_ROLE = "relay_migrator"
SCHEMA_DIR = Path(__file__).resolve().parent.parent / "db"


def database_exists(admin_conn: psycopg.Connection, name: str) -> bool:
    row = admin_conn.execute("SELECT 1 AS found FROM pg_database WHERE datname = %s", (name,)).fetchone()
    return row is not None


def create_database(admin_conn: psycopg.Connection, name: str, *, reset: bool = False) -> None:
    """Create database `name` if missing. With reset=True, drop it first.

    CREATE and DROP DATABASE cannot run inside a transaction, which is fine: the admin
    connection is autocommit. WITH (FORCE) disconnects anyone still connected, so a reset
    works while an API container holds a connection.
    """
    identifier = sql.Identifier(name)
    if reset:
        admin_conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(identifier))
    if not database_exists(admin_conn, name):
        admin_conn.execute(sql.SQL("CREATE DATABASE {}").format(identifier))


def ensure_migrator_role(admin_conn: psycopg.Connection, password: str) -> None:
    """Create the relay_migrator role if missing, and set its password either way.

    Roles belong to the whole Postgres server, not to one database, so the dev and test
    databases share this one role. The attributes say what it is NOT: not a superuser, and
    not able to create databases, create roles, or replicate. Setting the password on every
    run keeps the role in step with .env if the value there changes.

    CREATE ROLE and ALTER ROLE cannot take bound parameters, so the password is written into
    the statement with `sql.Literal`, which quotes it safely.
    """
    role = sql.Identifier(MIGRATOR_ROLE)
    exists = admin_conn.execute("SELECT 1 AS found FROM pg_roles WHERE rolname = %s", (MIGRATOR_ROLE,)).fetchone()
    if exists is None:
        admin_conn.execute(
            sql.SQL(
                "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD {}"
            ).format(role, sql.Literal(password))
        )
    else:
        admin_conn.execute(sql.SQL("ALTER ROLE {} PASSWORD {}").format(role, sql.Literal(password)))


def close_relay_db_to_other_roles(admin_conn: psycopg.Connection, relay_db_name: str) -> None:
    """Take away every ordinary role's right to connect to the Relay database.

    By default Postgres lets any role connect to any database (the grant is to PUBLIC, which
    means "everyone"). Revoking it leaves only superusers, which is what Relay itself uses
    (decision D15), so relay_migrator has no path to workflow state, leases, or the audit log.
    This lives here rather than in relay_schema.sql because the statement names the database,
    and the tests use a different name (`relay_test`). REVOKE is safe to repeat.
    """
    admin_conn.execute(
        sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(sql.Identifier(relay_db_name))
    )


def relay_schema_applied(relay_conn: psycopg.Connection) -> bool:
    """True if the `migrations` table already exists in this database."""
    row = relay_conn.execute("SELECT to_regclass('public.migrations') AS t").fetchone()
    return row["t"] is not None


def apply_relay_schema(admin_dsn: str, relay_db_name: str) -> bool:
    """Run db/relay_schema.sql unless the tables already exist. Returns True if it ran.

    The schema file uses plain CREATE TABLE, so running it twice would fail. Skipping when the
    tables exist is what makes `docker compose up` safe to repeat while the data volume lives on.
    A schema change therefore requires --reset, never an in-place upgrade (D17).
    """
    dsn = make_conninfo(admin_dsn, dbname=relay_db_name)
    with db.connect(dsn) as conn:
        if relay_schema_applied(conn):
            return False
        # No parameters, so psycopg sends the whole file as one multi-statement string.
        conn.execute((SCHEMA_DIR / "relay_schema.sql").read_text(encoding="utf-8"))
        return True


def apply_target_schema(admin_dsn: str, target_db_name: str, *, force: bool = False) -> bool:
    """Run db/target_schema.sql if the target has no `relay_applied` table yet. Returns True
    if it ran.

    The file DROPS the demo tables and the markers before recreating them. On a target that
    already holds applied migrations that would erase them while Relay still says SUCCEEDED,
    so the normal path never re-runs it (decision D34). `force=True` runs it regardless: the
    tests use that as their per-test reset, where wiping the target is the point.

    The whole file runs in one transaction, so a failure part-way leaves the target as it was.
    """
    dsn = make_conninfo(admin_dsn, dbname=target_db_name)
    with db.connect(dsn) as conn:
        already = conn.execute("SELECT to_regclass('public.relay_applied') AS t").fetchone()["t"] is not None
        if already and not force:
            return False
        with conn.transaction():
            # DROP TABLE needs an exclusive lock. If some leftover session still holds the
            # tables, fail after 10 seconds rather than wait forever.
            conn.execute("SET LOCAL lock_timeout = '10s'")
            conn.execute((SCHEMA_DIR / "target_schema.sql").read_text(encoding="utf-8"))
        return True


def init_databases(
    admin_dsn: str,
    relay_db_name: str,
    target_db_name: str,
    migrator_password: str,
    *,
    reset: bool = False,
) -> None:
    with db.connect(admin_dsn) as admin_conn:
        create_database(admin_conn, relay_db_name, reset=reset)
        create_database(admin_conn, target_db_name, reset=reset)
        ensure_migrator_role(admin_conn, migrator_password)
        close_relay_db_to_other_roles(admin_conn, relay_db_name)
    apply_relay_schema(admin_dsn, relay_db_name)
    # After a reset the target database is brand new, so this always runs then.
    apply_target_schema(admin_dsn, target_db_name)


def main() -> None:
    parser = argparse.ArgumentParser(description="Create Relay's databases, target role, and schemas.")
    parser.add_argument("--reset", action="store_true", help="drop both databases first (destroys data)")
    args = parser.parse_args()

    init_databases(load_admin_dsn(), RELAY_DB_NAME, TARGET_DB_NAME, load_migrator_password(), reset=args.reset)
    print(f"dbinit: {RELAY_DB_NAME} and {TARGET_DB_NAME} are ready" + (" (reset)" if args.reset else ""))


if __name__ == "__main__":
    main()
