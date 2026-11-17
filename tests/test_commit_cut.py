"""The target connection is lost during COMMIT itself (DESIGN.md section 1 #8, section 4.3
"Crash during COMMIT"; added after the review, REVIEW.md section 5 and finding F9).

COMMIT is the one statement whose failure the client cannot interpret. If the connection
dies while the worker waits for the answer, two things may have happened, and from the
worker's side they look identical:

- "answer lost":    the server received COMMIT and made the transaction durable; only the
                    reply never reached the worker. The migration IS applied.
- "never arrived":  the connection died before COMMIT reached the server. Postgres rolled
                    the transaction back. The migration is NOT applied.

So the worker must not guess. It writes nothing to Relay (outcome `abandoned`), its lease
runs out, and the next attempt's marker insert finds out which of the two it was.

How the cut is made: `CommitCuttingProxy` below, a small TCP relay that stands between the
worker and Postgres. (TCP is the network connection Postgres clients use; a relay or
"proxy" here is a program that accepts the worker's connection, opens its own connection
to the real server, and copies bytes both ways.) It copies everything untouched until it
sees the COMMIT message, then cuts the connection at exactly that moment, on whichever
side the test asked for. Killing the server session (`pg_terminate_backend`, used in
`test_execute.py`) cannot do this: it cannot be aimed at the instant between "COMMIT sent"
and "answer received". The proxy uses only the standard library (decision D38).
"""

import socket
import threading

import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from relay import worker
from relay.sqlhash import sha256_hex
from tests.helpers import (
    THREAD_TIMEOUT,
    Background,
    columns_of,
    events_of,
    expire_lease,
    markers_of,
    seed_migration,
    wait_until_migrator_sessions_are_gone,
)

ADD_NICKNAME = "ALTER TABLE public.customers ADD COLUMN nickname text"

# How psycopg ends `with conn.transaction():`: one simple-query message whose text is
# "COMMIT" followed by a zero byte. Nothing else the worker sends contains these bytes.
COMMIT_MESSAGE = b"COMMIT\x00"

ANSWER_LOST = "answer_lost"        # COMMIT reaches the server and is processed; the reply is dropped
NEVER_ARRIVED = "never_arrived"    # the connection is cut before COMMIT reaches the server


class CommitCuttingProxy:
    """A TCP relay between the worker and Postgres that cuts the connection at COMMIT.

    Each accepted connection gets two copying loops: client -> server (in `_serve`) and
    server -> client (a second thread). When the client -> server loop sees COMMIT:

    - mode ANSWER_LOST:   forward the COMMIT, wait until the server has answered (so the
                          commit is certainly done), throw the answer away, close both sides.
    - mode NEVER_ARRIVED: close both sides without forwarding it.

    `cut` is set once that has happened, so a test can assert the cut really took place:
    if the proxy ever missed the COMMIT, the test fails instead of passing for the wrong reason.
    """

    def __init__(self, upstream_host: str, upstream_port: int, mode: str):
        self.upstream = (upstream_host, upstream_port)
        self.mode = mode
        self.cut = threading.Event()
        self._server_answered = threading.Event()
        self._listener = socket.socket()
        self._listener.bind(("127.0.0.1", 0))      # port 0: the operating system picks a free port
        self._listener.listen()
        self.port = self._listener.getsockname()[1]
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def close(self) -> None:
        self._listener.close()                     # makes accept() fail, which ends the accept loop

    def _accept_loop(self) -> None:
        while True:
            try:
                client, _address = self._listener.accept()
            except OSError:
                return                             # the listener was closed
            threading.Thread(target=self._serve, args=(client,), daemon=True).start()

    def _serve(self, client: socket.socket) -> None:
        server = socket.create_connection(self.upstream)
        drop_replies = threading.Event()

        def server_to_client():
            while True:
                try:
                    data = server.recv(65536)
                except OSError:
                    return
                if not data:
                    return
                if drop_replies.is_set():
                    # This is the server's answer to COMMIT. The client must never see it.
                    self._server_answered.set()
                    return
                try:
                    client.sendall(data)
                except OSError:
                    return

        threading.Thread(target=server_to_client, daemon=True).start()
        try:
            while True:
                data = client.recv(65536)
                if not data:
                    break
                if COMMIT_MESSAGE in data and not self.cut.is_set():
                    if self.mode == ANSWER_LOST:
                        drop_replies.set()
                        server.sendall(data)
                        assert self._server_answered.wait(THREAD_TIMEOUT), "the server never answered COMMIT"
                    self.cut.set()
                    break                          # falls through to closing both sides
                server.sendall(data)
        finally:
            for sock in (client, server):
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                sock.close()


@pytest.fixture
def cut_at_commit(settings):
    """Start a CommitCuttingProxy in front of the target database.

    `cut_at_commit(mode)` returns (proxy, dsn), where `dsn` is the worker's usual target
    connection string with host and port swapped for the proxy's. Proxies are closed after
    the test.
    """
    proxies = []

    def start(mode: str):
        info = conninfo_to_dict(settings.target_db_dsn)
        proxy = CommitCuttingProxy(info["host"], int(info.get("port") or 5432), mode)
        proxies.append(proxy)
        return proxy, make_conninfo(settings.target_db_dsn, host="127.0.0.1", port=proxy.port)

    yield start
    for proxy in proxies:
        proxy.close()


def row_of(conn, migration_id) -> dict:
    return conn.execute(
        "SELECT status, error, lease_token FROM migrations WHERE id = %s", (migration_id,)
    ).fetchone()


def test_commit_processed_but_answer_lost_is_not_failed_and_next_worker_finds_the_marker(
    conn, target, make_worker, cut_at_commit
):
    """Guarantee: when the target COMMITS but the acknowledgement never reaches the worker,
    the worker writes nothing to Relay (outcome `abandoned`; the job stays EXECUTING under
    its lease, no error). The target holds the marker and the column. After the lease
    expires, the next worker's marker insert finds the marker, runs no second DDL, and marks
    the job SUCCEEDED.

    Invariant: 4 (the committed marker with the approved hash is what counts as success),
    and DESIGN.md section 1 #8: never mark FAILED on an unknown outcome.
    How the loss is forced: the first worker reaches the target through a proxy that forwards
    COMMIT, waits for the server's answer, drops it, and closes the connection.
    """
    migration_id = seed_migration(conn, status="APPROVED", sql=ADD_NICKNAME)
    proxy, proxied_dsn = cut_at_commit(ANSWER_LOST)
    w1 = make_worker("w1", target_db_dsn=proxied_dsn)

    outcome = Background(w1.run_once).join()

    assert proxy.cut.is_set(), "the proxy never saw a COMMIT, so nothing was cut"
    assert outcome == worker.ABANDONED
    # The target DID commit; Relay does not know.
    assert markers_of(target) == {migration_id: sha256_hex(ADD_NICKNAME)}
    assert "nickname" in columns_of(target, "customers")
    row = row_of(conn, migration_id)
    assert (row["status"], row["error"], row["lease_token"] is not None) == ("EXECUTING", None, True)

    # The next attempt settles it. A second ADD COLUMN would fail with "duplicate column"
    # and mark the job FAILED, so SUCCEEDED here also shows the DDL was not run again.
    expire_lease(conn, migration_id)
    assert make_worker("w2").run_once() == worker.SUCCEEDED
    assert row_of(conn, migration_id)["status"] == "SUCCEEDED"
    assert markers_of(target) == {migration_id: sha256_hex(ADD_NICKNAME)}
    assert events_of(conn, migration_id) == [
        ("APPROVED", "EXECUTING", "worker:w1"),
        ("EXECUTING", "APPROVED", "reclaimer:w2"),
        ("APPROVED", "EXECUTING", "worker:w2"),
        ("EXECUTING", "SUCCEEDED", "worker:w2"),
    ]


def test_commit_that_never_arrived_is_not_failed_and_next_worker_applies_it(
    conn, target, make_worker, cut_at_commit
):
    """Guarantee: when the connection dies before COMMIT reaches the target, the worker again
    writes nothing to Relay (outcome `abandoned`, job still EXECUTING, no error), and the
    target keeps neither marker nor column, because Postgres rolled the transaction back.
    After the lease expires, the next worker applies the migration for real.

    Invariant: 4 (marker and DDL are one transaction: both or neither), and DESIGN.md
    section 1 #8. Together with the test above: the worker sees the SAME thing in both cases
    and does the same thing, and the outcome is right in both.
    How the loss is forced: the proxy closes the connection when COMMIT arrives, without
    forwarding it.
    """
    migration_id = seed_migration(conn, status="APPROVED", sql=ADD_NICKNAME)
    proxy, proxied_dsn = cut_at_commit(NEVER_ARRIVED)
    w1 = make_worker("w1", target_db_dsn=proxied_dsn)

    outcome = Background(w1.run_once).join()

    assert proxy.cut.is_set(), "the proxy never saw a COMMIT, so nothing was cut"
    assert outcome == worker.ABANDONED
    wait_until_migrator_sessions_are_gone(target)     # the server has rolled the dead session back
    assert markers_of(target) == {}
    assert "nickname" not in columns_of(target, "customers")
    row = row_of(conn, migration_id)
    assert (row["status"], row["error"], row["lease_token"] is not None) == ("EXECUTING", None, True)

    expire_lease(conn, migration_id)
    assert make_worker("w2").run_once() == worker.SUCCEEDED
    assert row_of(conn, migration_id)["status"] == "SUCCEEDED"
    assert markers_of(target) == {migration_id: sha256_hex(ADD_NICKNAME)}
    assert "nickname" in columns_of(target, "customers")
