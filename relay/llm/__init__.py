"""The LLM interface (DESIGN.md section 6.1).

The worker only ever sees what is defined here: a Proposal, two error types, and one method.
A real provider (Groq, Phase 2) plugs in behind `get_llm` without touching the worker.

Whatever an adapter returns is untrusted: the worker always runs it through the SQL policy.
"""

from dataclasses import dataclass
from typing import Protocol

RISK_LEVELS = ("LOW", "MEDIUM", "HIGH")


@dataclass(frozen=True)
class Proposal:
    sql: str
    risk_level: str   # one of RISK_LEVELS; reviewer context only, no code branches on it (D20)
    risk_reason: str


class LLMTransientError(Exception):
    """Worth retrying: a timeout, a rate limit, a provider 5xx."""


class LLMPermanentError(Exception):
    """Retrying will not help: the model's answer is unusable."""


class LLMClient(Protocol):
    """Anything with this method can be the worker's LLM. Adapters map their provider's
    errors onto the two exception types above and own their own timeout."""

    def propose(self, description: str, schema_summary: str) -> Proposal: ...


def get_llm(settings) -> LLMClient:
    """Pick the adapter named by LLM_PROVIDER. Unknown names fail at startup."""
    if settings.llm_provider == "fake":
        from relay.llm.fake import FakeLLM

        return FakeLLM()
    raise ValueError(f"unknown LLM_PROVIDER {settings.llm_provider!r} (only 'fake' exists until Phase 2)")
