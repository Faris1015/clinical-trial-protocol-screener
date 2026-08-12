"""Shared LangGraph state for the TrialGate screening pipeline.

Every node reads from and writes partial updates to this state. The `events`
field uses an append reducer so all nodes can log without clobbering each other
— it is the data source for the frontend's live execution view.
"""

import operator
from datetime import UTC
from typing import Annotated, Literal, TypedDict

from app.services.usage import LlmCall

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
    # Terminal, and a *decision* rather than a breakdown (#91): a reviewer
    # judged the protocol unscreenable and stopped the run at the gate. Distinct
    # from "failed", which is the pipeline breaking — the funnel counts them
    # separately because "we chose not to screen this" and "we could not" are
    # different answers to the same question.
    "rejected",
]


class AgentEvent(TypedDict):
    agent: str  # "router" | "parser" | "critic" | "matcher" | "human"
    # "started" | "completed" | "rejected" | "escalated" | "failed", plus the
    # human-gate outcomes: "approved" (#50), "edited" (#53) and "rejected" (#91).
    # `rejected` is shared with the Critic's push-back — the `agent` is what says
    # which of the two a log entry is.
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

    # The deterministic rules this run is judged against (#97), snapshotted into
    # the state when the run enters the graph and refreshed when a revised run
    # re-enters it.
    #
    # In the state rather than read by the Critic node itself, because rules now
    # live in a table (`persistence.RuleStore`) and the node is synchronous —
    # there is no way for it to await a query. Passing them in is not merely the
    # workaround, though, it is the better contract: the Critic's parse→critic
    # loop can run several times, and a rule retired between two attempts of the
    # *same run* would otherwise change the verdict halfway through. A snapshot
    # makes one run one rule set, and puts the rules a run was judged by in its
    # own checkpoint, where an auditor reading a year-old run can still see them.
    compliance_rules: list[dict]

    # Plain-language layer (#52). `compliance_summary` is the Critic's verdict in
    # one sentence a non-technical reviewer can act on; each finding carries its
    # own `explanation` alongside the technical `message`. Distinct from
    # `critic_feedback`, which is written for the Parser LLM and stays technical.
    compliance_summary: str

    # Human-in-the-loop gate audit trail (#50). Written when a reviewer clears the
    # gate, *before* the matcher resumes — so the identity that authorized
    # touching patient data is durable in the checkpoint even if matching then
    # fails. PHI-safe by construction: staff identity and a timestamp, never
    # anything about a patient.
    approved_by: str | None
    approved_by_role: str | None
    approved_at: str | None

    # The other exit from the gate (#91), written the same way and for the same
    # reason: a reviewer who judges a protocol unscreenable stops the run, and
    # who said so — and why — is persisted *before* the graph terminates. The
    # reason is required, so a rejected run is never a bare dead end. PHI-safe by
    # the same construction: it is about the protocol, never about a patient.
    rejected_by: str | None
    rejected_by_role: str | None
    rejected_at: str | None
    rejected_reason: str | None

    # Edit-and-rerun at the gate (#53). `criteria_revision` is 0 for the parser's
    # own extraction and increments once per reviewer revision — it is the
    # optimistic-concurrency token that stops a second reviewer's stale edit from
    # silently overwriting the first's. `criteria_edits` uses the same append
    # reducer as `events`, so each revision's before/after diff joins a log the
    # run detail view can replay rather than replacing its predecessor.
    criteria_revision: int
    criteria_edits: Annotated[list[CriteriaEdit], operator.add]

    # Matcher output. `match_summary` (#52) is the cohort split in one plain
    # sentence; each evaluation carries a per-patient `summary` and a per-criterion
    # `explanation` next to the raw pass/fail statuses.
    matched_patients: list[dict]
    match_summary: str

    # The term mappings this run resolved (#96) — see `matcher.serialize_verdicts`
    # for the shape and why it is stored filtered.
    #
    # Kept for the same reason #95 kept `observed` on quantitative verdicts: it is
    # what makes the run's categorical half re-derivable without a model. Reverse
    # matching (a patient against every approved run) scores patients the run never
    # saw, and the ambiguous tail of a categorical criterion is exactly the part it
    # cannot settle by itself. Without this the choice would be to call an LLM on a
    # read-only page or to guess, and the second is worse.
    #
    # PHI-safe by the standard the rest of the checkpoint already meets: these are
    # clinical *terms* drawn from the cohort's records, the same vocabulary
    # `matched_patients` already quotes back in every explanation beside it.
    term_mappings: dict

    # Observability
    events: Annotated[list[AgentEvent], operator.add]
    current_step: ScreeningStatus

    # What the models cost (#101). One entry per LLM call — node, provider,
    # model, prompt/completion tokens and estimated micro-USD (see
    # services/usage.py) — appended by the graph's `_instrument` wrapper through
    # the same reducer `events` uses, so the parse/critic loop's repeated calls
    # accumulate rather than overwrite.
    #
    # It lives in the checkpoint rather than in a counter because a screening's
    # bill spans two requests: the stream that parks the run at the gate and the
    # approval that resumes it into the Matcher. Only durable state bridges them —
    # and it is what lets the run detail view report a cost for a run that
    # finished last week. PHI-safe by construction: token counts and a price,
    # never prompt or completion text.
    llm_usage: Annotated[list[LlmCall], operator.add]


def event(agent: str, status: str, detail: str) -> AgentEvent:
    from datetime import datetime

    return AgentEvent(
        agent=agent,
        status=status,
        detail=detail,
        timestamp=datetime.now(UTC).isoformat(),
    )


def initial_state(
    raw_protocol_text: str,
    source_filename: str,
    compliance_rules: list[dict] | None = None,
) -> ScreenerState:
    """Build the fresh state a screening starts from.

    The graph's first checkpoint is written from this when a run streams, so
    it is rebuilt from the durable store (not held in process memory) — a
    restart between upload and stream loses nothing.

    `compliance_rules` is the enabled rule set as of the moment the run starts
    (#97); `stream_screening` reads it from the rules table. It defaults to empty
    rather than being required so a caller building a bare state — the fakes in
    the test suite, chiefly — needs no rules table to do it. An empty set means
    the deterministic layer finds nothing, which is the safe direction: the
    semantic layer still runs and the run still stops at the human gate.
    """
    return ScreenerState(
        raw_protocol_text=raw_protocol_text,
        source_filename=source_filename,
        parsed_criteria=None,
        compliance_passed=False,
        critic_feedback=None,
        parse_attempts=0,
        compliance_findings=[],
        compliance_rules=list(compliance_rules or []),
        compliance_summary="",
        approved_by=None,
        approved_by_role=None,
        approved_at=None,
        rejected_by=None,
        rejected_by_role=None,
        rejected_at=None,
        rejected_reason=None,
        criteria_revision=0,
        criteria_edits=[],
        matched_patients=[],
        match_summary="",
        term_mappings={},
        events=[],
        current_step="routing",
        llm_usage=[],
    )
