import pytest

from tests.helpers import seed_migration


@pytest.mark.parametrize("run", ["first", "second"])
def test_database_is_empty_at_start_of_each_test(conn, run):
    """Guarantee: every test starts with empty relay tables, whatever ran before it.

    Invariant: "no test depends on another" (CLAUDE.md, Repo practices). Both runs insert one
    row and expect to see exactly one. If the reset fixture broke, the second run would see two.
    """
    count_at_start = conn.execute("SELECT count(*) AS n FROM migrations").fetchone()["n"]
    assert count_at_start == 0

    seed_migration(conn)

    count_after = conn.execute("SELECT count(*) AS n FROM migrations").fetchone()["n"]
    assert count_after == 1
