"""Database connections (DESIGN.md conventions and decision D1).

Every connection is opened with autocommit=True. That means NO transaction is open unless the
code writes an explicit `with conn.transaction():` block. psycopg's default is the opposite: it
silently opens a transaction on the first statement and keeps it open, which could hold a row
lock across a slow call and break invariant 2.
"""

import psycopg
from psycopg.rows import dict_row

CONNECT_TIMEOUT_SECONDS = 5


def connect(dsn: str) -> psycopg.Connection:
    """Open one autocommit connection whose rows come back as dicts.

    The isolation level is pinned to READ COMMITTED rather than left to the server's default.
    With this set, every `with conn.transaction():` block starts with
    `BEGIN ISOLATION LEVEL READ COMMITTED`. The transition helper depends on that level: the
    loser of a race gets "0 rows" only because a waiting UPDATE re-checks its WHERE clause
    after the winner commits (decision D27).
    """
    conn = psycopg.connect(
        dsn,
        autocommit=True,
        row_factory=dict_row,
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
    )
    conn.isolation_level = psycopg.IsolationLevel.READ_COMMITTED
    return conn
