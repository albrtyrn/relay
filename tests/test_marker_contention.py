"""Two workers run the target transaction for the SAME migration at the same time
(DESIGN.md section 4.3 "Conflict", M4).

This should not normally happen (a lease gives one worker the job), but it does when a slow
worker's lease expires and a second worker takes over while the first is still inside its
target transaction. The marker's primary key is what keeps the DDL from running twice: the
second INSERT has to wait until the first transaction finishes, and then either finds the
marker (skip) or finds nothing (apply).
"""

import threading
import uuid

import pytest

from relay import db, hooks
from relay.sqlhash import sha256_hex
from relay.target import ALREADY_APPLIED, APPLIED, apply_on_target
from tests.helpers import THREAD_TIMEOUT, Background, columns_of, markers_of, wait_until_blocked

ADD_NICKNAME = "ALTER TABLE public.customers ADD COLUMN nickname text"
NICKNAME_HASH = sha256_hex(ADD_NICKNAME)


class PauseFirstCaller:
    """A hook that stops the first thread to reach it and lets every later one through.

    Hooks are process-wide and both workers run the same line. `then_raise` makes the paused
    thread raise when released, which rolls its transaction back (a stand-in for a crash
    before commit).
    """

    def __init__(self, then_raise: bool = False):
        self.paused = threading.Event()
        self.release = threading.Event()
        self._then_raise = then_raise
        self._lock = threading.Lock()
        self._used = False

    def __call__(self):
        with self._lock:
            first = not self._used
            self._used = True
        if not first:
            return
        self.paused.set()
        assert self.release.wait(THREAD_TIMEOUT), "test never released the first worker"
        if self._then_raise:
            raise RuntimeError("simulated crash before the target commit")


def test_second_inserter_waits_then_skips_when_first_commits(settings, target):
    """Guarantee: while worker A holds an uncommitted marker, worker B's marker insert WAITS;
    when A commits, B gets no row back, sees the matching hash, and skips the DDL. One
    marker, one column, and the DDL ran once.

    Invariant: 4. How the race is forced: A is paused by a hook inside its target transaction
    right after its marker insert; B is then started and observed in `pg_stat_activity`
    waiting on a lock (really blocked, not just slow); only then is A released. No sleeps.
    If B did not wait, it would either run the DDL too (a "column already exists" error) or
    read no marker; both would fail this test.
    """
    migration_id = uuid.uuid4()
    pause = PauseFirstCaller()
    hooks.register("execute.after_marker_insert", pause)

    with db.connect(settings.target_db_dsn) as conn_a, db.connect(settings.target_db_dsn) as conn_b:
        thread_a = Background(lambda: apply_on_target(conn_a, migration_id, ADD_NICKNAME, NICKNAME_HASH, settings))
        try:
            assert pause.paused.wait(THREAD_TIMEOUT), "worker A never reached the hook"
            thread_b = Background(lambda: apply_on_target(conn_b, migration_id, ADD_NICKNAME, NICKNAME_HASH, settings))
            wait_until_blocked(target, conn_b.info.backend_pid)
            assert markers_of(target) == {}        # A has not committed: nobody else sees its marker
        finally:
            pause.release.set()
        result_a = thread_a.join()
        result_b = thread_b.join()

    assert (result_a, result_b) == (APPLIED, ALREADY_APPLIED)
    assert markers_of(target) == {migration_id: NICKNAME_HASH}
    assert "nickname" in columns_of(target, "customers")


def test_second_inserter_applies_when_first_rolls_back(settings, target):
    """Guarantee: if worker A's target transaction rolls back (it died before commit), the
    waiting worker B inserts the marker itself and runs the DDL. The migration is applied
    once, by B.

    Invariant: 4. A's marker and DDL vanish together, so B is not fooled by a marker for
    work that never committed. Forced the same way as the test above, except that A raises
    when released, which rolls its transaction back.
    """
    migration_id = uuid.uuid4()
    pause = PauseFirstCaller(then_raise=True)
    hooks.register("execute.after_marker_insert", pause)

    with db.connect(settings.target_db_dsn) as conn_a, db.connect(settings.target_db_dsn) as conn_b:
        thread_a = Background(lambda: apply_on_target(conn_a, migration_id, ADD_NICKNAME, NICKNAME_HASH, settings))
        try:
            assert pause.paused.wait(THREAD_TIMEOUT), "worker A never reached the hook"
            thread_b = Background(lambda: apply_on_target(conn_b, migration_id, ADD_NICKNAME, NICKNAME_HASH, settings))
            wait_until_blocked(target, conn_b.info.backend_pid)
        finally:
            pause.release.set()
        with pytest.raises(RuntimeError, match="simulated crash"):
            thread_a.join()
        result_b = thread_b.join()

    assert result_b == APPLIED
    assert markers_of(target) == {migration_id: NICKNAME_HASH}
    assert "nickname" in columns_of(target, "customers")
