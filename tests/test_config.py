"""Settings, and the startup check that the lease outlasts the target transaction
(DESIGN.md section 7.6, decision D33; M4).

A lease is the time a worker is given to finish a job. If the target transaction could run
longer than the lease, a healthy worker could be reclaimed while it is still applying DDL,
and two workers would be inside the target for the same job. The marker makes that safe,
but it also widens the late-apply gap (DESIGN.md section 1 #5). So a worker refuses to start
unless LEASE_SECONDS > lock_timeout + statement_timeout + a safety margin.
"""

from dataclasses import replace

import pytest

from relay import config
from relay.llm.fake import FakeLLM
from relay.worker import Worker


def test_default_settings_pass_the_lease_check(settings):
    """Guarantee: the defaults are consistent: a 60 s lease against 5 s + 15 s + the margin.

    Invariant: none of the six; decision D33.
    """
    config.check_lease_outlasts_target_work(settings)     # does not raise


@pytest.mark.parametrize(
    ("lease_seconds", "lock_timeout", "statement_timeout"),
    [
        (30, "5s", "15s"),      # exactly equal to 5 + 15 + 10: "longer than" means strictly longer
        (29, "5s", "15s"),
        (60, "30s", "30s"),
        (60, "1min", "15s"),
        (11, "500ms", "500ms"),
    ],
)
def test_lease_not_longer_than_target_timeouts_is_refused(settings, lease_seconds, lock_timeout, statement_timeout):
    """Guarantee: a lease that is not strictly longer than lock_timeout + statement_timeout +
    the safety margin is rejected, with a message that names the numbers.

    Invariant: narrows the late-apply gap (decisions D23, D33). The marker insert can wait up
    to lock_timeout and the DDL can run up to statement_timeout, so their sum bounds the
    target transaction's statements; the margin covers connecting and the commit.
    """
    bad = replace(settings, lease_seconds=lease_seconds, lock_timeout=lock_timeout, statement_timeout=statement_timeout)

    with pytest.raises(ValueError, match="LEASE_SECONDS"):
        config.check_lease_outlasts_target_work(bad)


def test_lease_just_longer_than_the_bound_is_accepted(settings):
    """Guarantee: the check is a strict "greater than" and nothing more: 31 s passes against
    5 s + 15 s + 10 s.

    Invariant: none of the six; pins the boundary of decision D33.
    """
    config.check_lease_outlasts_target_work(replace(settings, lease_seconds=31))


def test_worker_refuses_to_start_with_a_lease_that_is_too_short(settings):
    """Guarantee: a Worker cannot even be constructed with such settings: it raises before
    opening any database connection, so a misconfigured worker never claims a job.

    Invariant: narrows the late-apply gap (decisions D23, D33).
    """
    with pytest.raises(ValueError, match="LEASE_SECONDS"):
        Worker(replace(settings, lease_seconds=20), FakeLLM(), worker_id="w1")


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("5s", 5.0), ("15s", 15.0), ("500ms", 0.5), ("2min", 120.0)],
)
def test_timeout_text_is_read_as_seconds(text, seconds):
    """Guarantee: timeout settings written as <whole number><ms|s|min> are understood.

    Invariant: none of the six. The same text is handed to Postgres unchanged, so the format
    is deliberately a small subset of what Postgres accepts.
    """
    assert config.timeout_seconds(text) == seconds


@pytest.mark.parametrize("text", ["", "5", "5 s", "0s", "0", "fast", "-5s", "1h", "1.5s"])
def test_unreadable_or_disabled_timeout_is_refused(settings, text):
    """Guarantee: a timeout the check cannot read, or one that is zero (which in Postgres
    means "no timeout at all"), is rejected at startup instead of silently passing.

    Invariant: narrows the late-apply gap. With a timeout disabled the target transaction
    has no upper bound, so no lease could be known to outlast it.
    """
    with pytest.raises(ValueError):
        config.check_lease_outlasts_target_work(replace(settings, statement_timeout=text))


def test_load_settings_reads_the_m4_variables():
    """Guarantee: the worker's target settings come from the environment, with the documented
    defaults when a variable is absent.

    Invariant: none of the six (DESIGN.md section 7.6).
    """
    base = {"RELAY_DB_DSN": "relay-dsn", "TARGET_DB_DSN": "target-dsn"}

    defaults = config.load_settings(base)
    custom = config.load_settings({
        **base, "LEASE_SECONDS": "90", "LOCK_TIMEOUT": "2s",
        "STATEMENT_TIMEOUT": "20s", "TARGET_IDLE_TIMEOUT": "45s",
    })

    assert (defaults.target_db_dsn, defaults.lock_timeout, defaults.statement_timeout, defaults.target_idle_timeout) == (
        "target-dsn", "5s", "15s", "30s",
    )
    assert (custom.lease_seconds, custom.lock_timeout, custom.statement_timeout, custom.target_idle_timeout) == (
        90, "2s", "20s", "45s",
    )


def test_load_settings_requires_the_target_dsn():
    """Guarantee: a missing TARGET_DB_DSN is a clear startup error naming the variable.

    Invariant: none of the six.
    """
    with pytest.raises(RuntimeError, match="TARGET_DB_DSN"):
        config.load_settings({"RELAY_DB_DSN": "relay-dsn"})
