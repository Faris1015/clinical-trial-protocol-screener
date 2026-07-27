"""Shared LangGraph state for the TrialGate screening pipeline.

Every node reads from and writes partial updates to this state. The `events`
field uses an append reducer so all nodes can log without clobbering each other
— it is the data source for the frontend's live execution view.
"""

import operator
from datetime import UTC
from typing import Annotated, Literal, TypedDict


class AgentEvent(TypedDict):
    agent: str  # "router" | "parser" | "critic" | "matcher"
    status: str  # "started" | "completed" | "rejected" | "escalated" | "failed"
    detail: str
    timestamp: str


class ScreenerState(TypedDict):
    # Input
    raw_protocol_text: str
    source_filename: str

    # Parser output (validated CriteriaSchema, dumped to dict)
    parsed_criteria: dict | None

    # Critic loop control
    compliance_passed: bool
    critic_feedback: str | None
    parse_attempts: int
    compliance_findings: list[dict]

    # Human-in-the-loop gate audit trail (#50). Written when a reviewer clears the
    # gate, *before* the matcher resumes — so the identity that authorized
    # touching patient data is durable in the checkpoint even if matching then
    # fails. PHI-safe by construction: staff identity and a timestamp, never
    # anything about a patient.
    approved_by: str | None
    approved_by_role: str | None
    approved_at: str | None

    # Matcher output
    matched_patients: list[dict]

    # Observability
    events: Annotated[list[AgentEvent], operator.add]
    current_step: Literal[
        "routing",
        "parsing",
        "critiquing",
        "awaiting_approval",
        "matching",
        "done",
        "failed",
        "escalated",
    ]


def event(agent: str, status: str, detail: str) -> AgentEvent:
    from datetime import datetime

    return AgentEvent(
        agent=agent,
        status=status,
        detail=detail,
        timestamp=datetime.now(UTC).isoformat(),
    )


def initial_state(raw_protocol_text: str, source_filename: str) -> ScreenerState:
    """Build the fresh state a screening starts from.

    The graph's first checkpoint is written from this when a run streams, so
    it is rebuilt from the durable store (not held in process memory) — a
    restart between upload and stream loses nothing.
    """
    return ScreenerState(
        raw_protocol_text=raw_protocol_text,
        source_filename=source_filename,
        parsed_criteria=None,
        compliance_passed=False,
        critic_feedback=None,
        parse_attempts=0,
        compliance_findings=[],
        approved_by=None,
        approved_by_role=None,
        approved_at=None,
        matched_patients=[],
        events=[],
        current_step="routing",
    )
