"""The deterministic fake LLM (DESIGN.md section 6.1, M3). No database in this file."""

import pytest

from relay.llm import LLMPermanentError, LLMTransientError, Proposal, get_llm
from relay.llm.fake import FakeLLM


def test_add_column_description_maps_to_expected_sql():
    """Guarantee: "add column <col> <type> to <table>" always yields the same ALTER TABLE,
    whatever the case or surrounding spaces of the description.

    Invariant: none of the six. Determinism is what lets every other test know the SQL (and
    so the hash) in advance.
    """
    proposal = FakeLLM().propose("  Add Column nickname TEXT to Customers ", schema_summary="ignored")

    assert proposal == Proposal(
        sql="ALTER TABLE public.customers ADD COLUMN nickname text",
        risk_level="LOW",
        risk_reason="adds a nullable column; no table rewrite",
    )


def test_fake_does_not_judge_the_table():
    """Guarantee: the fake happily writes SQL for a table the policy forbids.

    Invariant: none of the six. This is deliberate: the LLM is never the safety check, so
    the policy-violation test needs an LLM that produces disallowed SQL.
    """
    proposal = FakeLLM().propose("add column x text to secrets", schema_summary="")

    assert proposal.sql == "ALTER TABLE public.secrets ADD COLUMN x text"


def test_directives_raise_transient_and_permanent():
    """Guarantee: `fake:transient` and `fake:permanent` raise the two error types, and any
    description the fake cannot parse is a permanent error.

    Invariant: none of the six. The worker treats the two types differently (retry vs.
    FAILED), so tests need a way to trigger each on demand.
    """
    llm = FakeLLM()

    with pytest.raises(LLMTransientError):
        llm.propose("fake:transient", schema_summary="")
    with pytest.raises(LLMPermanentError):
        llm.propose("fake:permanent", schema_summary="")
    with pytest.raises(LLMPermanentError):
        llm.propose("make the database faster", schema_summary="")


def test_get_llm_returns_the_fake_and_rejects_unknown_providers(settings):
    """Guarantee: LLM_PROVIDER=fake gives the fake; an unknown provider fails at startup
    instead of at the first job.

    Invariant: none of the six.
    """
    from dataclasses import replace

    assert isinstance(get_llm(settings), FakeLLM)
    with pytest.raises(ValueError):
        get_llm(replace(settings, llm_provider="no-such-provider"))
