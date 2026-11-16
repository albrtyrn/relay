"""A deterministic fake LLM (DESIGN.md section 6.1). Stateless: same input, same output.

It turns one sentence shape into SQL and has directives for forcing each error type:

    add column <col> <type> to <table>   ->  ALTER TABLE public.<table> ADD COLUMN <col> <type>
    fake:transient                       ->  raises LLMTransientError
    fake:permanent                       ->  raises LLMPermanentError
    anything else                        ->  raises LLMPermanentError

It does NOT check the table or the type. That is deliberate: the LLM is never the safety
check. "add column x text to secrets" yields SQL the policy rejects, and "add column email
text to customers" yields allowed SQL that fails on the target (the column exists).
"""

import re

from relay.llm import LLMPermanentError, LLMTransientError, Proposal

_ADD_COLUMN = re.compile(r"^add column (?P<column>\S+) (?P<type>.+) to (?P<table>\S+)$")


class FakeLLM:
    def propose(self, description: str, schema_summary: str) -> Proposal:
        text = description.strip().lower()
        if text == "fake:transient":
            raise LLMTransientError("fake LLM: transient failure requested")
        if text == "fake:permanent":
            raise LLMPermanentError("fake LLM: permanent failure requested")

        match = _ADD_COLUMN.match(text)
        if match is None:
            raise LLMPermanentError("fake LLM cannot parse description")
        return Proposal(
            sql=f"ALTER TABLE public.{match['table']} ADD COLUMN {match['column']} {match['type']}",
            risk_level="LOW",
            risk_reason="adds a nullable column; no table rewrite",
        )
