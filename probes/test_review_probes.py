"""Review probes: experiments from the skeptical review (REVIEW.md).

What is left here are `measure_...` probes: they are NOT tests of a guarantee, they only
print what they observe.

Everything else became real tests on 2026-11-16:
- the probes that asserted what DESIGN.md says should happen (the reclaim races, the COMMIT
  cut, the timeouts firing, the mid-flight executor): tests/test_reclaim_races.py,
  tests/test_commit_cut.py, tests/test_timeouts_fire.py, and
  tests/test_stale_worker.py::test_stale_executor_cannot_overwrite_a_new_owner_mid_flight;
- the F1 finding (retry limit decided by a read that did not wait), fixed:
  tests/test_retry_limit.py;
- the F3 finding (no timeouts on Relay connections), fixed: tests/test_relay_timeouts.py.

Run with: docker compose run --rm tests python -m pytest probes -s
"""

import uuid
from collections import Counter

import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo

from relay import policy, service
from relay.api import create_app
from tests.helpers import SEED_HASH, run_behind_barrier, seed_migration


# ---------------------------------------------------------------- measurements


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
