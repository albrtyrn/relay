"""Create the databases and apply the schema (DESIGN.md sections 2 and 7.2, decision D17).

    python -m relay.dbinit            create anything missing, never drop (safe on every `up`)
    python -m relay.dbinit --reset    DROP both databases first, then rebuild (destroys data)

The test suite calls `init_databases` too, with test database names, so tests build their
databases exactly the way the real stack does.

M1 creates an empty target_db. The target schema and the relay_migrator role arrive in M4.
"""

import argparse
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from relay import db
from relay.config import load_admin_dsn

RELAY_DB_NAME = "relay_db"
TARGET_DB_NAME = "target_db"
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


def init_databases(admin_dsn: str, relay_db_name: str, target_db_name: str, *, reset: bool = False) -> None:
    with db.connect(admin_dsn) as admin_conn:
        create_database(admin_conn, relay_db_name, reset=reset)
        create_database(admin_conn, target_db_name, reset=reset)
    apply_relay_schema(admin_dsn, relay_db_name)


def main() -> None:
    parser = argparse.ArgumentParser(description="Create Relay's databases and apply the schema.")
    parser.add_argument("--reset", action="store_true", help="drop both databases first (destroys data)")
    args = parser.parse_args()

    init_databases(load_admin_dsn(), RELAY_DB_NAME, TARGET_DB_NAME, reset=args.reset)
    print(f"dbinit: {RELAY_DB_NAME} and {TARGET_DB_NAME} are ready" + (" (reset)" if args.reset else ""))


if __name__ == "__main__":
    main()
