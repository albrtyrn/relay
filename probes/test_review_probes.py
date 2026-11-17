"""Review probes: experiments for the cases PROGRESS.md lists as untested.

Each probe ASSERTS what DESIGN.md says should happen, so a failure is a finding.
Probes named `finding_...` assert the BAD outcome on purpose, to demonstrate it.
"""

import socket
import threading
import time
import uuid
from collections import Counter

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from relay import db, hooks, policy, service, worker
from relay.api import create_app
from relay.claim import claim_one, reclaim_expired
from relay.sqlhash import sha256_hex
from relay.transitions import transition
from tests.helpers import (
    SEED_HASH,
    THREAD_TIMEOUT,
    Background,
    FixedLLM,
    columns_of,
    events_of,
    expire_lease,
    markers_of,
    run_behind_barrier,
    seed_migration,
    wait_until_blocked,
)

ADD = "ALTER TABLE public.customers ADD COLUMN nickname text"
CLEAR = {"lease_token": None, "lease_expires_at": None}


def row_of(conn, mid):
    return conn.execute(
        "SELECT status, error, lease_token, attempts FROM migrations WHERE id = %s", (mid,)
    ).fetchone()


# ---------------------------------------------------------------- 1. two reclaimers, one row


def test_p1a_two_reclaimers_one_expired_row_forced(conn, settings):
    """R1 holds the row (reclaim inside an open outer transaction); R2 sweeps at that moment."""
    mid = seed_migration(conn, status="DRAFTING")
    expire_lease(conn, mid)
    with db.connect(settings.relay_db_dsn) as r1, db.connect(settings.relay_db_dsn) as r2:
        r2.execute("SET lock_timeout = '3s'")
        with r1.transaction():
            assert reclaim_expired(r1, "r1") == 1
            assert reclaim_expired(r2, "r2") == 0      # skipped, did not wait
            assert row_of(conn, mid)["status"] == "DRAFTING"   # r1 not committed yet
    assert row_of(conn, mid)["status"] == "PENDING"
    assert events_of(conn, mid) == [("DRAFTING", "PENDING", "reclaimer:r1")]


def test_p1b_eight_reclaimers_forty_rows_barrier(conn, settings):
    ids = []
    for n in range(40):
        mid = seed_migration(conn, status="DRAFTING" if n % 2 else "EXECUTING")
        expire_lease(conn, mid)
        ids.append(mid)

    def sweep(name):
        def action(own):
            total = 0
            while (n := reclaim_expired(own, name)) > 0:
                total += n
            return total
        return action

    counts = run_behind_barrier(settings.relay_db_dsn, [sweep(f"r{n}") for n in range(8)])
    print("\nP1b reclaimed per reclaimer:", counts)
    assert sum(counts) == 40
    per_row = conn.execute("SELECT migration_id, count(*) AS n FROM migration_events GROUP BY migration_id").fetchall()
    assert {r["migration_id"]: r["n"] for r in per_row} == {mid: 1 for mid in ids}
    assert conn.execute("SELECT count(*) AS n FROM migrations WHERE lease_token IS NOT NULL").fetchone()["n"] == 0


# ---------------------------------------------------------------- 2. reclaimer vs the owner's save


def test_p2a_owner_save_in_flight_reclaimer_skips(conn, settings):
    """The owner's save transaction is open (row locked); the reclaimer sweeps at that moment."""
    mid = seed_migration(conn, status="DRAFTING")
    token = conn.execute("SELECT lease_token FROM migrations WHERE id = %s", (mid,)).fetchone()["lease_token"]
    expire_lease(conn, mid)
    with db.connect(settings.relay_db_dsn) as owner, db.connect(settings.relay_db_dsn) as reclaimer:
        reclaimer.execute("SET lock_timeout = '3s'")
        with owner.transaction():
            assert transition(
                owner, mid, "DRAFTING", "WAITING_FOR_APPROVAL", "worker:owner", lease_token=token,
                set_fields={"proposed_sql": ADD, "proposed_sql_hash": sha256_hex(ADD), "risk_level": "LOW",
                            "risk_reason": "r", "attempts": 0, **CLEAR},
            )
            assert reclaim_expired(reclaimer, "r") == 0
    assert row_of(conn, mid)["status"] == "WAITING_FOR_APPROVAL"
    assert events_of(conn, mid) == [("DRAFTING", "WAITING_FOR_APPROVAL", "worker:owner")]


def test_p2b_reclaim_in_flight_owner_save_waits_then_loses(conn, settings, make_worker):
    """Through the real worker: A is paused before its save; the reclaim transaction is open
    (uncommitted) when A is released; A must block, then lose once the reclaim commits."""
    mid = seed_migration(conn, status="PENDING", description="add column nickname text to customers")
    a = make_worker("a")
    paused, go = threading.Event(), threading.Event()

    def pause():
        paused.set()
        assert go.wait(THREAD_TIMEOUT)

    hooks.register("draft.before_save", pause)
    thread_a = Background(a.run_once)
    assert paused.wait(THREAD_TIMEOUT)
    expire_lease(conn, mid)
    with db.connect(settings.relay_db_dsn) as r:
        with r.transaction():
            assert reclaim_expired(r, "r") == 1
            go.set()
            wait_until_blocked(conn, a.conn.info.backend_pid)      # A really waits on the row lock
            assert row_of(conn, mid)["status"] == "DRAFTING"
    assert thread_a.join() == worker.LOST
    assert row_of(conn, mid)["status"] == "PENDING"
    assert events_of(conn, mid) == [("PENDING", "DRAFTING", "worker:a"), ("DRAFTING", "PENDING", "reclaimer:r")]


def test_p2c_reclaim_rolls_back_owner_save_proceeds(conn, settings, make_worker):
    mid = seed_migration(conn, status="PENDING", description="add column nickname text to customers")
    a = make_worker("a")
    paused, go = threading.Event(), threading.Event()

    def pause():
        paused.set()
        assert go.wait(THREAD_TIMEOUT)

    hooks.register("draft.before_save", pause)
    thread_a = Background(a.run_once)
    assert paused.wait(THREAD_TIMEOUT)
    expire_lease(conn, mid)

    class Boom(Exception):
        pass

    with db.connect(settings.relay_db_dsn) as r:
        with pytest.raises(Boom):
            with r.transaction():
                assert reclaim_expired(r, "r") == 1
                go.set()
                wait_until_blocked(conn, a.conn.info.backend_pid)
                raise Boom
    assert thread_a.join() == worker.SAVED
    assert row_of(conn, mid)["status"] == "WAITING_FOR_APPROVAL"


def test_p2d_barrier_owner_save_vs_reclaimers_many_rows(conn, settings):
    """Unforced but massed: 30 expired DRAFTING rows; one thread saves each as owner while 3
    reclaimers sweep. Every row must end with exactly one event and a consistent state."""
    rows = []
    for _ in range(30):
        mid = seed_migration(conn, status="DRAFTING")
        token = conn.execute("SELECT lease_token FROM migrations WHERE id = %s", (mid,)).fetchone()["lease_token"]
        expire_lease(conn, mid)
        rows.append((mid, token))

    def owner(own):
        results = {}
        for mid, token in rows:
            with own.transaction():
                results[mid] = transition(
                    own, mid, "DRAFTING", "WAITING_FOR_APPROVAL", "worker:owner", lease_token=token,
                    set_fields={"proposed_sql": ADD, "proposed_sql_hash": sha256_hex(ADD), "risk_level": "LOW",
                                "risk_reason": "r", "attempts": 0, **CLEAR},
                )
        return results

    def sweeper(name):
        def action(own):
            total = 0
            for _ in range(200):
                total += reclaim_expired(own, name, batch_size=3)
            return total
        return action

    results = run_behind_barrier(settings.relay_db_dsn, [owner, sweeper("r1"), sweeper("r2"), sweeper("r3")])
    saved = sum(1 for ok in results[0].values() if ok)
    reclaimed = sum(results[1:])
    print(f"\nP2d owner saved {saved}, reclaimers took {reclaimed}")
    assert saved + reclaimed == 30
    for mid, _ in rows:
        events = events_of(conn, mid)
        assert len(events) == 1
        status = row_of(conn, mid)["status"]
        assert status == events[0][1]
        assert (status == "WAITING_FOR_APPROVAL") == results[0][mid]


# ---------------------------------------------------------------- 3. connection lost during COMMIT

COMMIT_MESSAGE = b"COMMIT\x00"


class CommitCuttingProxy:
    """A TCP relay between the worker and Postgres that cuts the connection at COMMIT.

    mode "ack_lost":      the COMMIT reaches the server and is processed; the answer never
                          reaches the client.
    mode "never_arrived": the connection dies just before the COMMIT reaches the server.
    """

    def __init__(self, host, port, mode):
        self.upstream = (host, int(port))
        self.mode = mode
        self.cut = threading.Event()
        self.server_answered = threading.Event()
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.port = self.listener.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                client, _ = self.listener.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(client,), daemon=True).start()

    def _serve(self, client):
        server = socket.create_connection(self.upstream)
        swallow = threading.Event()

        def server_to_client():
            while True:
                try:
                    data = server.recv(65536)
                except OSError:
                    return
                if not data:
                    return
                if swallow.is_set():
                    self.server_answered.set()
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
                    self.cut.set()
                    if self.mode == "ack_lost":
                        swallow.set()
                        server.sendall(data)
                        assert self.server_answered.wait(THREAD_TIMEOUT)
                    break
                server.sendall(data)
        finally:
            for s in (client, server):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                s.close()


def proxied_dsn(settings, mode):
    info = conninfo_to_dict(settings.target_db_dsn)
    proxy = CommitCuttingProxy(info["host"], info.get("port", 5432), mode)
    return proxy, make_conninfo(settings.target_db_dsn, host="127.0.0.1", port=proxy.port)


def wait_for_no_migrator_sessions(target):
    deadline = time.monotonic() + THREAD_TIMEOUT
    while time.monotonic() < deadline:
        n = target.execute("SELECT count(*) AS n FROM pg_stat_activity WHERE usename = 'relay_migrator'").fetchone()["n"]
        if n == 0:
            return
        time.sleep(0.01)
    raise AssertionError("migrator session still there")


def test_p3a_commit_processed_but_ack_lost(conn, target, settings, make_worker):
    mid = seed_migration(conn, status="APPROVED", sql=ADD)
    proxy, dsn = proxied_dsn(settings, "ack_lost")
    w1 = make_worker("w1", target_db_dsn=dsn)

    outcome = Background(w1.run_once).join()

    assert proxy.cut.is_set(), "the proxy never saw a COMMIT"
    print("\nP3a outcome:", outcome)
    assert outcome == worker.ABANDONED
    # The target DID commit; Relay does not know.
    assert markers_of(target) == {mid: sha256_hex(ADD)}
    assert "nickname" in columns_of(target, "customers")
    row = row_of(conn, mid)
    assert (row["status"], row["error"], row["lease_token"] is not None) == ("EXECUTING", None, True)

    expire_lease(conn, mid)
    assert make_worker("w2").run_once() == worker.SUCCEEDED       # finds the marker, no second DDL
    assert row_of(conn, mid)["status"] == "SUCCEEDED"
    assert events_of(conn, mid)[-1] == ("EXECUTING", "SUCCEEDED", "worker:w2")


def test_p3b_commit_never_arrived(conn, target, settings, make_worker):
    mid = seed_migration(conn, status="APPROVED", sql=ADD)
    proxy, dsn = proxied_dsn(settings, "never_arrived")
    w1 = make_worker("w1", target_db_dsn=dsn)

    outcome = Background(w1.run_once).join()

    assert proxy.cut.is_set()
    print("\nP3b outcome:", outcome)
    assert outcome == worker.ABANDONED
    wait_for_no_migrator_sessions(target)
    assert markers_of(target) == {}
    assert "nickname" not in columns_of(target, "customers")
    assert row_of(conn, mid)["status"] == "EXECUTING"

    expire_lease(conn, mid)
    assert make_worker("w2").run_once() == worker.SUCCEEDED       # applies it for real this time
    assert markers_of(target) == {mid: sha256_hex(ADD)}


# ---------------------------------------------------------------- 4. the timeouts actually firing


def test_p4a_lock_timeout_fires_during_ddl(conn, target, make_worker):
    mid = seed_migration(conn, status="APPROVED", sql=ADD)
    w1 = make_worker("w1", lock_timeout="300ms")
    with target.transaction():
        target.execute("LOCK TABLE public.customers IN ACCESS SHARE MODE")   # e.g. a long-running read
        started = time.monotonic()
        outcome = w1.run_once()
        waited = time.monotonic() - started
    row = row_of(conn, mid)
    print(f"\nP4a outcome {outcome} after {waited:.2f}s, error = {row['error']!r}")
    assert outcome == worker.FAILED
    assert "55P03" in row["error"]
    assert markers_of(target) == {} and "nickname" not in columns_of(target, "customers")


def test_p4b_statement_timeout_fires_during_ddl(conn, target, make_worker):
    mid = seed_migration(conn, status="APPROVED", sql=ADD)
    w1 = make_worker("w1", lock_timeout="5s", statement_timeout="300ms")
    with target.transaction():
        target.execute("LOCK TABLE public.customers IN ACCESS SHARE MODE")
        started = time.monotonic()
        outcome = w1.run_once()
        waited = time.monotonic() - started
    row = row_of(conn, mid)
    print(f"\nP4b outcome {outcome} after {waited:.2f}s, error = {row['error']!r}")
    assert outcome == worker.FAILED
    assert "57014" in row["error"]
    assert markers_of(target) == {} and "nickname" not in columns_of(target, "customers")


def test_p4c_lock_timeout_fires_on_the_marker_step(conn, target, make_worker):
    """Another transaction holds an uncommitted marker for the same id longer than lock_timeout."""
    mid = seed_migration(conn, status="APPROVED", sql=ADD)
    w1 = make_worker("w1", lock_timeout="300ms")
    with pytest.raises(RuntimeError, match="undo"):
        with target.transaction():
            target.execute("INSERT INTO relay_applied (migration_id, sql_hash) VALUES (%s, %s)", (mid, sha256_hex(ADD)))
            outcome = w1.run_once()
            raise RuntimeError("undo")          # the other transaction rolls back afterwards
    row = row_of(conn, mid)
    print(f"\nP4c outcome {outcome}, row = {row['status']}, error = {row['error']!r}")
    assert outcome == worker.ABANDONED
    assert (row["status"], row["error"]) == ("EXECUTING", None)
    assert markers_of(target) == {}
    expire_lease(conn, mid)
    assert w1.run_once() == worker.SUCCEEDED


def test_p4d_idle_in_transaction_timeout_fires(conn, target, make_worker):
    """The worker stalls inside its target transaction for longer than the idle timeout."""
    mid = seed_migration(conn, status="APPROVED", sql=ADD)
    w1 = make_worker("w1", target_idle_timeout="300ms")
    hooks.register("execute.after_marker_insert", lambda: wait_for_no_migrator_sessions(target))
    outcome = w1.run_once()
    row = row_of(conn, mid)
    print(f"\nP4d outcome {outcome}, row = {row['status']}, error = {row['error']!r}")
    assert outcome == worker.ABANDONED
    assert (row["status"], row["error"]) == ("EXECUTING", None)
    assert markers_of(target) == {} and "nickname" not in columns_of(target, "customers")


# ---------------------------------------------------------------- 5. findings


def test_finding_retry_limit_reads_marker_without_waiting(conn, target, migrator, make_worker):
    """A stale attempt is INSIDE its target transaction (marker inserted, not committed) when
    a later attempt, now over the retry limit, looks at the marker. The read does not wait,
    sees nothing, and marks FAILED. The stale attempt then commits."""
    mid = seed_migration(conn, status="APPROVED", sql=ADD)
    conn.execute("UPDATE migrations SET attempts = 1 WHERE id = %s", (mid,))    # one attempt already used
    b = make_worker("b", max_attempts=1)
    with migrator.transaction():                                               # the stale attempt "A"
        migrator.execute("INSERT INTO relay_applied (migration_id, sql_hash) VALUES (%s, %s)", (mid, sha256_hex(ADD)))
        migrator.execute(ADD)
        outcome = b.run_once()
    row = row_of(conn, mid)
    print(f"\nFINDING retry-limit: worker outcome {outcome}; Relay = {row['status']} ({row['error']}); "
          f"target marker = {mid in markers_of(target)}, column = {'nickname' in columns_of(target, 'customers')}")
    assert outcome == worker.FAILED
    assert row["status"] == "FAILED"
    assert markers_of(target) == {mid: sha256_hex(ADD)}          # applied on the target
    assert "nickname" in columns_of(target, "customers")


def test_contrast_under_the_limit_the_marker_insert_waits(conn, target, migrator, make_worker):
    """Same situation, but attempts are under the limit: the normal path's marker INSERT waits
    on the stale attempt's marker, times out, and writes nothing (safe)."""
    mid = seed_migration(conn, status="APPROVED", sql=ADD)
    b = make_worker("b", lock_timeout="300ms")
    with migrator.transaction():
        migrator.execute("INSERT INTO relay_applied (migration_id, sql_hash) VALUES (%s, %s)", (mid, sha256_hex(ADD)))
        migrator.execute(ADD)
        outcome = b.run_once()
    assert outcome == worker.ABANDONED
    assert row_of(conn, mid)["status"] == "EXECUTING"


def test_measure_which_step_decides_the_20_way_approval_race(conn, settings):
    """How many of the 19 losers actually reach the compare-and-set (step 2), and how many are
    turned away by the plain read (step 1)? The message text tells them apart."""
    tally = Counter()
    zero_cas_runs = 0
    for run in range(30):
        mid = seed_migration(conn, status="WAITING_FOR_APPROVAL")

        def approver(name):
            def action(own):
                with own.transaction():
                    return service.approve(own, mid, SEED_HASH, actor=f"user:{name}")
            return action

        results = run_behind_barrier(settings.relay_db_dsn, [approver(n) for n in range(20)])
        cas = sum(1 for code, body in results if code == 409 and "no longer" in body["detail"])
        read = sum(1 for code, body in results if code == 409 and "no longer" not in body["detail"])
        assert cas + read == 19
        tally["lost at the compare-and-set"] += cas
        tally["lost at the plain read"] += read
        zero_cas_runs += cas == 0
    print(f"\nMEASURE 30 runs x 19 losers: {dict(tally)}; runs where NO loser reached the compare-and-set: {zero_cas_runs}")


def test_measure_relay_connections_have_no_timeouts(conn):
    values = {
        name: conn.execute("SELECT current_setting(%s) AS v", (name,)).fetchone()["v"]
        for name in ("lock_timeout", "statement_timeout", "idle_in_transaction_session_timeout", "tcp_user_timeout")
    }
    print("\nMEASURE relay connection timeouts:", values)


def test_finding_frozen_reclaimer_blocks_the_owner_forever(conn, settings):
    """Nothing bounds how long a Relay transaction may hold a row lock. If a process freezes
    inside reclaim, the owner's save waits with no timeout."""
    mid = seed_migration(conn, status="DRAFTING")
    token = conn.execute("SELECT lease_token FROM migrations WHERE id = %s", (mid,)).fetchone()["lease_token"]
    expire_lease(conn, mid)
    with db.connect(settings.relay_db_dsn) as frozen, db.connect(settings.relay_db_dsn) as owner:
        with frozen.transaction():
            reclaim_expired(frozen, "frozen")

            def save():
                with owner.transaction():
                    return transition(owner, mid, "DRAFTING", "FAILED", "worker:o", lease_token=token,
                                      set_fields={"error": "x", **CLEAR})

            thread = Background(save)
            wait_until_blocked(conn, owner.info.backend_pid)
            time.sleep(2)   # observation only: still blocked two seconds later, nothing times it out
            state = conn.execute(
                "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s", (owner.info.backend_pid,)
            ).fetchone()["wait_event_type"]
            print("\nFINDING owner's save after 2s behind a frozen reclaim:", state)
            assert state == "Lock"
        assert thread.join() is False


def test_measure_500_body_has_no_stack_trace(settings):
    from dataclasses import replace
    dead = replace(settings, relay_db_dsn=make_conninfo(settings.relay_db_dsn, host="127.0.0.1", port=1))
    client = TestClient(create_app(dead), raise_server_exceptions=False)
    response = client.post(f"/migrations/{uuid.uuid4()}/approve", json={"sql_hash": SEED_HASH}, headers={"X-User": "a"})
    print("\nMEASURE 500:", response.status_code, repr(response.text))
    assert response.status_code == 500
    assert "Traceback" not in response.text and "psycopg" not in response.text


@pytest.mark.parametrize("sql", [
    'ALTER TABLE customers ADD COLUMN "abc\n" text',
    "ALTER TABLE customers ADD COLUMN c text -- trailing comment",
    "ALTER TABLE customers ADD COLUMN c text;",
    "ALTER TABLE customers ADD COLUMN c numeric(5000, 2)",
    "ALTER TABLE customers ADD COLUMN xmin text",
    "ALTER TABLE customers ADD c text",
    "ALTER TABLE customers * ADD COLUMN c text",
    "ALTER TABLE ONLY customers ADD COLUMN c text",
])
def test_measure_policy_edges(sql):
    try:
        policy.validate(sql)
        verdict = "ALLOWED"
    except policy.PolicyViolation as error:
        verdict = f"rejected ({error.rule})"
    print(f"\nMEASURE policy {sql!r}: {verdict}")


def test_measure_executor_token_matters_mid_flight(conn, target, make_worker):
    """Is there an EXECUTING analogue of the mid-draft token test? A commits on the target and
    pauses; B reclaims and re-claims (EXECUTING under B's token, B not finished); A writes."""
    mid = seed_migration(conn, status="APPROVED", sql=ADD)
    a = make_worker("a")
    paused, go = threading.Event(), threading.Event()

    def pause():
        paused.set()
        assert go.wait(THREAD_TIMEOUT)

    hooks.register("execute.after_target_commit", pause)
    thread_a = Background(a.run_once)
    assert paused.wait(THREAD_TIMEOUT)
    expire_lease(conn, mid)
    assert reclaim_expired(conn, "b") == 1
    claim_b = claim_one(conn, "b", 60)
    go.set()
    assert thread_a.join() == worker.LOST
    row = row_of(conn, mid)
    assert (row["status"], row["lease_token"]) == ("EXECUTING", claim_b.lease_token)
