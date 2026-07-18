"""Structured-output contracts for the LLM intelligence layers (#16).

Two hooks extend the graph beyond its deterministic core:

- `SemanticReview` backs the Critic's layer-2 pass — contradictions and
  omissions the YAML rules can't express, returned as `Finding`s that merge
  into the same `compliance_findings` list the deterministic layer feeds.
- `TermMapping` backs the Matcher's categorical term resolution — semantic
  equivalence between a protocol criterion and a patient's recorded terms, for
  the ambiguous tail that word-boundary matching can't settle on its own.

`Finding.rule_id` is deliberately absent from the schema: the Critic stamps
every semantic finding with `rule_id="LLM-SEM"` itself, so a model can't invent
its own identifiers.
"""

from typing import Literal

from pydantic import BaseModel, Field


class Finding(BaseModel):
    """One semantic issue surfaced by the Critic's LLM review pass."""

    severity: Literal["reject", "warn"] = Field(
        description="'reject' blocks screening and loops the Parser; 'warn' is advisory only"
    )
    message: str = Field(description="Human-readable description of the issue and where it is")


class SemanticReview(BaseModel):
    """The Critic's layer-2 output: zero or more semantic findings."""

    findings: list[Finding] = Field(
        default_factory=list,
        description="Empty when the extraction is semantically sound",
    )


TermVerdict = Literal["match", "no_match", "uncertain"]


class TermMatch(BaseModel):
    """Whether one patient term satisfies the criterion under review."""

    term: str = Field(description="The patient term being judged, echoed verbatim")
    verdict: TermVerdict = Field(
        description="'match' if the term satisfies the criterion, 'no_match' if it clearly "
        "does not, 'uncertain' if it cannot be decided confidently from the term alone"
    )


class TermMapping(BaseModel):
    """Per-term verdicts mapping one categorical criterion onto a set of patient terms."""

    results: list[TermMatch] = Field(default_factory=list)
