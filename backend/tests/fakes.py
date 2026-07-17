"""Shared integration-test doubles (#9).

`FakeChatModel` stands in for the real chat model so graph and API tests run
deterministically and free — no Ollama, no Anthropic, no network. The Parser is
the only node that touches the LLM, and it only ever does:

    get_llm().with_structured_output(CriteriaSchema).invoke(messages)

so we implement exactly that surface. A test scripts a sequence of extractions;
each `invoke()` replays the next one, and the last entry repeats so an
"always-bad" run can loop until the graph escalates without running off the end.

The criteria builders below produce a *good* extraction (passes the deterministic
Critic) and a *bad* one (trips RENAL-001: a vague renal criterion with no numeric
eGFR threshold), which is what drives the Critic→Parser loop in tests.
"""

from __future__ import annotations

from typing import Any

from app.schemas.criteria import CriteriaSchema

# Router admits input that is >= 200 chars and mentions an eligibility marker;
# it must NOT mention pregnancy keywords or the Critic's PREG-001 rule trips
# (protocol text without a matching condition criterion is a blocking finding).
PROTOCOL_TEXT = (
    "Phase II single-arm study of an investigational agent in adults.\n\n"
    "Inclusion criteria:\n"
    "- Age 18 years or older at the time of consent.\n"
    "- Adequate bone marrow and organ function per investigator assessment.\n\n"
    "Exclusion criteria:\n"
    "- Any concurrent condition that, in the opinion of the investigator, would\n"
    "  compromise the safety of the participant or the integrity of the study.\n"
    "- Participation in another interventional trial within the prior 30 days.\n"
)

# Fails the Router: too short and no eligibility section.
NON_PROTOCOL_TEXT = "Minutes from the weekly logistics sync. Coffee supplies are low."


def good_criteria(title: str = "Passing Trial") -> CriteriaSchema:
    """An extraction the deterministic Critic accepts (no blocking findings)."""
    return CriteriaSchema(
        trial_title=title,
        inclusion_quantitative=[
            {
                "attribute": "age",
                "operator": ">=",
                "value": 18,
                "value_high": None,
                "unit": "years",
                "source_text": "Age 18 years or older at the time of consent.",
            }
        ],
        inclusion_categorical=[],
        exclusion_quantitative=[],
        exclusion_categorical=[],
        unparseable=[],
    )


def bad_criteria(title: str = "Failing Trial") -> CriteriaSchema:
    """An extraction the Critic rejects: renal function left as vague language
    (RENAL-001 requires a numeric eGFR threshold)."""
    return CriteriaSchema(
        trial_title=title,
        inclusion_quantitative=[
            {
                "attribute": "age",
                "operator": ">=",
                "value": 18,
                "value_high": None,
                "unit": "years",
                "source_text": "Age 18 years or older at the time of consent.",
            }
        ],
        inclusion_categorical=[],
        exclusion_quantitative=[],
        exclusion_categorical=[],
        unparseable=["Adequate renal function."],
    )


class FakeChatModel:
    """Deterministic stand-in for the Parser's chat model.

    `scripted` is a list of `CriteriaSchema` (or `Exception`) replayed one per
    `invoke()`. The final entry repeats, so a loop that keeps getting the same
    bad extraction terminates via the graph's escalation cap, not an IndexError.
    """

    def __init__(self, scripted: list[Any]):
        assert scripted, "FakeChatModel needs at least one scripted output"
        self.scripted = list(scripted)
        self.calls = 0

    def with_structured_output(self, _schema: object, **_kwargs: object) -> FakeChatModel:
        return self

    def invoke(self, messages: object, *_args: object, **_kwargs: object) -> Any:
        item = self.scripted[min(self.calls, len(self.scripted) - 1)]
        self.calls += 1
        if isinstance(item, Exception):
            raise item
        return item
