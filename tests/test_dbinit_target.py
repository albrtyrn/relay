"""How dbinit treats the target schema (decision D34; M4)."""

import uuid

from relay.dbinit import apply_target_schema
from tests.conftest import TARGET_TEST_DB
from tests.helpers import SEED_HASH, columns_of, insert_marker, markers_of


def test_dbinit_does_not_rebuild_a_target_that_already_has_its_schema(admin_dsn, target):
    """Guarantee: the normal dbinit path (what `docker compose up` runs every time) leaves an
    existing target alone: applied columns and their markers survive.

    Invariant: supports 4. db/target_schema.sql starts by DROPPING the demo tables and
    `relay_applied`. Running it on every start would erase applied migrations while Relay
    still said SUCCEEDED. Only an explicit rebuild (`force=True`, used by the test reset and
    by `dbinit --reset`) may do that.
    """
    migration_id = uuid.uuid4()
    target.execute("ALTER TABLE public.customers ADD COLUMN applied_earlier text")
    insert_marker(target, migration_id, SEED_HASH)

    ran = apply_target_schema(admin_dsn, TARGET_TEST_DB)

    assert ran is False
    assert "applied_earlier" in columns_of(target, "customers")
    assert markers_of(target) == {migration_id: SEED_HASH}


def test_forced_rebuild_restores_the_original_target(admin_dsn, target):
    """Guarantee: a forced rebuild returns the target to its starting point: original
    columns, seed rows, no markers. This is the per-test reset.

    Invariant: 5's companion rule, "tests reset database state; no test depends on another".
    """
    target.execute("ALTER TABLE public.customers ADD COLUMN leftover text")
    insert_marker(target, uuid.uuid4(), SEED_HASH)

    ran = apply_target_schema(admin_dsn, TARGET_TEST_DB, force=True)

    assert ran is True
    assert columns_of(target, "customers") == {"id", "email", "full_name", "created_at"}
    assert markers_of(target) == {}
    assert target.execute("SELECT count(*) AS n FROM customers").fetchone()["n"] == 2
