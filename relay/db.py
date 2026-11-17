"""Database connections (DESIGN.md conventions and decisions D1, D40).

Every connection is opened with autocommit=True. That means NO transaction is open unless the
code writes an explicit `with conn.transaction():` block. psycopg's default is the opposite: it
silently opens a transaction on the first statement and keeps it open, which could hold a row
lock across a slow call and break invariant 2.

Two ways to connect:
- `connect_relay(settings)`: for the API and the workers talking to relay_db. Adds two
  session-wide timeouts, so a frozen process cannot hold a row lock, or wait for one, forever.
- `connect(dsn)`: everything else (the target, dbinit, /health, tests). No session timeouts;
  the target transaction sets its own, per transaction (relay/target.py).
"""

import psycopg
from psycopg.rows import dict_row

from relay.config import Settings, timeout_seconds

CONNECT_TIMEOUT_SECONDS = 5


def connect(dsn: str, *, options: str | None = None) -> psycopg.Connection:
    """Open one autocommit connection whose rows come back as dicts.

    The isolation level is pinned to READ COMMITTED rather than left to the server's default.
    With this set, every `with conn.transaction():` block starts with
    `BEGIN ISOLATION LEVEL READ COMMITTED`. The transition helper depends on that level: the
    loser of a race gets "0 rows" only because a waiting UPDATE re-checks its WHERE clause
    after the winner commits (decision D27).

    `options` is passed to the server at connection start (libpq's `options` parameter, for
    example "-c lock_timeout=5s"); `connect_relay` uses it.
    """
    extra = {} if options is None else {"options": options}
    conn = psycopg.connect(
        dsn,
        autocommit=True,
        row_factory=dict_row,
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
        **extra,
    )
    conn.isolation_level = psycopg.IsolationLevel.READ_COMMITTED
    return conn


def connect_relay(settings: Settings) -> psycopg.Connection:
    """Open a connection to relay_db for the API or a worker, with two limits that hold for
    the whole session (decision D40):

    - lock_timeout (RELAY_LOCK_TIMEOUT): a statement that waits this long for a row lock
      fails with SQLSTATE 55P03 instead of waiting forever. Nothing it tried was changed.
      The API turns that into 503; a worker abandons its lease.
    - idle_in_transaction_session_timeout (RELAY_IDLE_TIMEOUT): a session that sits inside
      an open transaction this long without sending anything is ended by the server and its
      transaction rolled back, which releases its locks. This is what removes a process
      that froze mid-transaction.

    The second limit only ever applies INSIDE a transaction. Because the connection is
    autocommit, a worker waiting on the LLM or the target, or sleeping between polls, has no
    transaction open and is never affected (invariant 2 is what makes this safe).

    The values are sent as startup options, so they are in force before the first statement
    and cost no extra round trip. They are checked first: only <whole number><ms|s|min> is
    accepted, and zero is refused, because 0 means "no timeout" in Postgres.
    """
    for name, value in (
        ("RELAY_LOCK_TIMEOUT", settings.relay_lock_timeout),
        ("RELAY_IDLE_TIMEOUT", settings.relay_idle_timeout),
    ):
        if timeout_seconds(value) <= 0:
            raise ValueError(f"{name} must be greater than zero; 0 disables the timeout in Postgres")
    options = (
        f"-c lock_timeout={settings.relay_lock_timeout} "
        f"-c idle_in_transaction_session_timeout={settings.relay_idle_timeout}"
    )
    return connect(settings.relay_db_dsn, options=options)
