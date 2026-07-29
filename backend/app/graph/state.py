"""Shared LangGraph state for the TrialGate screening pipeline.

Every node reads from and writes partial updates to this state. The `events`
field uses an append reducer so all nodes can log without clobbering each other
— it is the data source for the frontend's live execution view.
"""

import operator
from datetime import UTC
from typing import Annotated, Literal, TypedDict

# The phases a screening moves through. Doubles as the list endpoint's status
# filter (#51): the store's `status` column is denormalized from `current_step`
# (see services.screening._status_from_snapshot), so one definition keeps the
# API's accepted filter values from drifting from the values it can ever store.
ScreeningStatus = Literal[
    "routing",
    "parsing",
    "critiquing",
    "awaiting_approval",
    "matching",
    "done",
    "failed",
    "escalated",
]


class AgentEvent(TypedDict):
    agent: str  # "router" | "parser" | "critic" | "matcher" | "human"
    # "started" | "completed" | "rejected" | "escalated" | "failed", plus the
    # human-gate outcomes: "approved" (#50) and "edited" (#53).
    status: str
    detail: str
    timestamp: str


class CriteriaChange(TypedDict):
    """One field-level difference between two revisions of the criteria (#53).

    `before`/`after` are rendered one-line labels rather than raw objects: this
    is an audit record read by humans, and a label survives a schema change to
    the criterion types without becoming unreadable. Exactly one side is None for
    an addition or a removal.
    """

    bucket: str  # destination bucket, or the only bucket for a same-bucket change
    from_bucket: str | None  # source bucket — set only when kind == "reclassified"
    kind: str  # "modified" | "added" | "removed" | "reclassified"
    before: str | None
    after: str | None


class CriteriaEdit(TypedDict):
    """A reviewer's revision of the extraction, with who made it and what changed.

    PHI-safe by construction, like the approval trail: protocol criteria, staff
    identity and a timestamp — nothing about a patient.
    """

    revision: int
    edited_by: str
    edited_by_role: str
    edited_at: str
    changes: list[CriteriaChange]


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

    # Edit-and-rerun at the gate (#53). `criteria_revision` is 0 for the parser's
    # own extraction and increments once per reviewer revision — it is the
    # optimistic-concurrency token that stops a second reviewer's stale edit from
    # silently overwriting the first's. `criteria_edits` uses the same append
    # reducer as `events`, so each revision's before/after diff joins a log the
    # run detail view can replay rather than replacing its predecessor.
    criteria_revision: int
    criteria_edits: Annotated[list[CriteriaEdit], operator.add]

    # Matcher output
    matched_patients: list[dict]

    # Observability
    events: Annotated[list[AgentEvent], operator.add]
    current_step: ScreeningStatus


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
        criteria_revision=0,
        criteria_edits=[],
        matched_patients=[],
        events=[],
        current_step="routing",
    )
