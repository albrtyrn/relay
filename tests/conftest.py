"""Database creation, per-test reset, and shared fixtures (DESIGN.md section 7.2).

Tests run against real Postgres in their own databases, `relay_test` and `target_test`, on the
same server as the dev databases, so they never touch dev data (decision D18).
"""

import os
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo

from relay import db, hooks
from relay.api import create_app
from relay.config import Settings
from relay.dbinit import init_databases
from relay.llm.fake import FakeLLM
from relay.worker import Worker

RELAY_TEST_DB = "relay_test"
TARGET_TEST_DB = "target_test"


@pytest.fixture(scope="session")
def settings() -> Settings:
    """Create fresh test databases once per session and return Settings that point at them.

    Uses the same function as `python -m relay.dbinit --reset`, so the tests build the schema
    exactly the way the real stack does.
    """
    admin_dsn = os.environ["ADMIN_DSN"]
    init_databases(admin_dsn, RELAY_TEST_DB, TARGET_TEST_DB, reset=True)
    return Settings(relay_db_dsn=make_conninfo(admin_dsn, dbname=RELAY_TEST_DB))


@pytest.fixture(autouse=True)
def reset_state(settings):
    """Empty the relay tables before every test, so no test depends on another.

    Reset happens before the test, not after, so a failed test's rows are still there to
    inspect (DESIGN.md section 7.2). The target database has no tables until M4.
    """
    with db.connect(settings.relay_db_dsn) as conn:
        conn.execute("TRUNCATE migrations, migration_events RESTART IDENTITY")


@pytest.fixture
def conn(settings):
    """A direct autocommit connection to relay_test, for seeding rows and checking results."""
    with db.connect(settings.relay_db_dsn) as connection:
        yield connection


@pytest.fixture
def client(settings) -> TestClient:
    """An HTTP client talking to the real app, wired to relay_test."""
    return TestClient(create_app(settings))


@pytest.fixture
def broken_client(settings) -> TestClient:
    """An HTTP client whose app cannot reach its database (port 1 refuses connections)."""
    dead = replace(settings, relay_db_dsn=make_conninfo(settings.relay_db_dsn, host="127.0.0.1", port=1))
    return TestClient(create_app(dead))


@pytest.fixture(autouse=True)
def clear_hooks():
    """Remove every hook a test registered, so one test's pause cannot leak into the next."""
    yield
    hooks.clear()


@pytest.fixture
def make_worker(settings):
    """Build in-process workers wired to relay_test; their connections are closed afterwards.

    `make_worker("w1")` uses the fake LLM. Pass `llm=` for a test double, and keyword
    overrides such as `max_attempts=2` to change a setting for that worker only.
    """
    workers = []

    def make(worker_id: str, llm=None, **overrides) -> Worker:
        worker = Worker(replace(settings, **overrides), llm or FakeLLM(), worker_id=worker_id)
        workers.append(worker)
        return worker

    yield make
    for worker in workers:
        worker.close()
