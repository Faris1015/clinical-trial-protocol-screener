"""The per-run event timeline (#55) — the append-only event log as an audit trail.

The graph already records every agent transition in `ScreenerState.events` (an
`operator.add` list, so nodes append without clobbering each other). Read raw it
is a flat sequence of strings; what an auditor asks of a run is narrower and
harder: *how many times did the Parser have to try, what did the Critic push
back, did it escalate, and who authorized touching patient data.* This module
derives that from the log rather than adding fields to the state, so it applies
identically to a run checkpointed months ago and to one that finished a second
ago.

Three decisions worth knowing before editing this module:

**Derived, never stored.** Nothing here is written back to the checkpoint. A
replay must not amend the run it is replaying (the same rule that keeps
`get_screening_report` from recording the exporter in state), and a derivation
can be corrected for every past run at once — a denormalized field can only be
corrected going forward.

**Rendered labels, not raw enums.** Entries carry `label`, `outcome` and
`elapsed` already in the form a human reads, the same convention
`CriteriaChange.before`/`after` follow (app/graph/state.py). The timeline is
rendered twice — in the run detail view and in the downloadable report
(services/report.py) — and a screen and the document exported from it must not
name the same step differently. The raw `agent`/`status`/`timestamp` ride along
for machine-readable attributes and for a caller that wants to re-render.

**Defensive about its input.** The values come from a checkpoint that may have
been written by an older build of the pipeline, by a run that failed partway, or
(in the report's case) from a hand-assembled payload. Every field is guarded:
a missing, null, or wrongly-typed entry degrades to an empty string or a zero
rather than raising on a read of a finished run.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, TypedDict

# Display names for the log's actors. Unnumbered, unlike the pipeline cards
# (frontend AgentCard's "2 · Parser"): the position of a step in a timeline is
# its position in the list, and a run that looped shows "Parser" three times.
_ACTOR_LABELS = {
    "router": "Router",
    "parser": "Parser",
    "critic": "Regulatory Critic",
    "matcher": "Patient Matcher",
    # Both human-gate outcomes — an approval (#50) and an edit-and-rerun (#53) —
    # are logged under the one `human` agent.
    "human": "Reviewer",
}

_OUTCOME_LABELS = {
    "started": "Started",
    "completed": "Completed",
    "rejected": "Rejected",
    "escalated": "Escalated",
    "failed": "Failed",
    "approved": "Approved",
    "edited": "Edited",
}

# The two agents inside the Critic→Parser retry loop. Only their entries are
# stamped with an attempt number: numbering the Router or the approval gate would
# imply they can repeat, and the point of the number is to make the loop legible.
_LOOP_AGENTS = frozenset({"parser", "critic"})


class TimelineEntry(TypedDict):
    """One step of a run, ready to render.

    `attempt` is the parse round this step belongs to, or 0 for a step outside
    the retry loop; `revision` is the reviewer revision an `edited` step produced,
    0 otherwise. `actor`/`actor_role` name the human behind a human step and are
    empty for machine steps — the identity is what makes this an audit record
    rather than a progress log.
    """

    seq: int
    agent: str
    label: str
    status: str
    outcome: str
    detail: str
    timestamp: str
    elapsed: str
    attempt: int
    revision: int
    actor: str
    actor_role: str


class TimelineSummary(TypedDict):
    """The run's shape in numbers — what a reviewer reads before the entries.

    `attempts` counts Parser *runs*, which is one more than the checkpoint's
    `parse_attempts` for a run whose last extraction failed (that counter only
    advances on a successful one). The approval fields come from the durable
    trail in state, not from the log, so they survive a checkpoint whose
    approval event is missing.
    """

    started_at: str
    ended_at: str
    duration: str
    attempts: int
    critic_rejections: int
    revisions: int
    escalated: bool
    approved_by: str
    approved_by_role: str
    approved_at: str


class RunTimeline(TypedDict):
    entries: list[TimelineEntry]
    summary: TimelineSummary


def _mapping(item: Any) -> Mapping[str, Any]:
    return item if isinstance(item, Mapping) else {}


def _list(values: Mapping[str, Any], key: str) -> list[Any]:
    value = values.get(key)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return list(value)
    return []


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _instant(iso: Any) -> datetime | None:
    """An event timestamp as an aware datetime, or None if it isn't one.

    Naive values are read as UTC — the pipeline only ever writes aware stamps
    (`state.event`), but a checkpoint hand-edited or migrated from an older build
    can hold a naive one, and mixing the two in a subtraction raises.
    """
    if not iso:
        return None
    try:
        parsed = datetime.fromisoformat(str(iso))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def format_span(milliseconds: float) -> str:
    """A duration in the coarsest unit that still says something.

    Milliseconds up to a second (node latency is the interesting figure there),
    then tenths of a second, then minutes, then hours — because the gap between
    the Critic clearing a run and a reviewer approving it is a wait measured in
    hours and "10883400ms" is not a reading of it.

    Negative input clamps to zero: a gap can only come out negative from clock
    skew or a hand-edited stamp, and that is noise rather than information.
    """
    total_ms = max(0, int(milliseconds))
    if total_ms < 1000:
        return f"{total_ms}ms"
    # Tenths of a second up to the minute. The boundary is 59.95s, not 60s, so a
    # value that would render as "60.0s" is reported as "1m 0s" instead.
    if total_ms < 59_950:
        return f"{total_ms / 1000:.1f}s"
    minutes, seconds = divmod(round(total_ms / 1000), 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def _gap(previous: datetime | None, current: datetime | None) -> str:
    """How long after the previous step this one happened, e.g. `+1.4s`.

    Empty for the first step and for either stamp being unreadable — a relative
    figure with nothing to be relative to is worse than none.
    """
    if previous is None or current is None:
        return ""
    return f"+{format_span((current - previous).total_seconds() * 1000)}"


def build_timeline(values: Mapping[str, Any]) -> RunTimeline:
    """One run's event log as a chronological audit trail.

    `values` is the checkpoint block of `GET /api/screenings/{id}/state`. Entries
    keep the log's own order rather than being re-sorted by timestamp: the append
    reducer records the order the graph actually executed in, and two steps of one
    node can share a stamp to the resolution `datetime.now` gives.

    Identity for a human step is correlated rather than parsed out of the event
    text: an approval reads `approved_by` from the durable trail (#50), and the
    Nth `edited` event pairs with the Nth `criteria_edits` record (#53) — both
    lists use the same append reducer and are written in the same update, so the
    Nth of one is the Nth of the other. A correlation that comes up empty leaves
    `actor` blank; the event's own `detail` still names who acted.
    """
    events = [_mapping(item) for item in _list(values, "events")]
    edits = [_mapping(item) for item in _list(values, "criteria_edits")]

    entries: list[TimelineEntry] = []
    attempt = 0
    edits_matched = 0
    previous: datetime | None = None

    for seq, raw in enumerate(events):
        agent = _text(raw.get("agent"))
        status = _text(raw.get("status"))
        timestamp = _text(raw.get("timestamp"))
        at = _instant(timestamp)

        # Every Parser event is one extraction run, so counting them reproduces
        # the Parser's own "(attempt N)" numbering — and the Critic entry that
        # follows belongs to the round that produced the extraction it reviewed.
        # An edit-and-rerun (#53) re-enters as the Parser without logging a
        # Parser event, which is right: it does not spend an automated attempt,
        # and the escalation cap is deliberately not reset for it.
        if agent == "parser":
            attempt += 1

        revision = 0
        actor = ""
        actor_role = ""
        if agent == "human" and status == "edited":
            record = _mapping(edits[edits_matched]) if edits_matched < len(edits) else {}
            edits_matched += 1
            revision = _int(record.get("revision"))
            actor = _text(record.get("edited_by"))
            actor_role = _text(record.get("edited_by_role"))
        elif agent == "human" and status == "approved":
            actor = _text(values.get("approved_by"))
            actor_role = _text(values.get("approved_by_role"))

        entries.append(
            TimelineEntry(
                seq=seq,
                agent=agent,
                label=_ACTOR_LABELS.get(agent, agent),
                status=status,
                outcome=_OUTCOME_LABELS.get(status, status),
                detail=_text(raw.get("detail")),
                timestamp=timestamp,
                elapsed=_gap(previous, at),
                attempt=attempt if agent in _LOOP_AGENTS else 0,
                revision=revision,
                actor=actor,
                actor_role=actor_role,
            )
        )
        # Only advance on a readable stamp, so one malformed timestamp costs its
        # own gap and not the next step's as well.
        if at is not None:
            previous = at

    return RunTimeline(entries=entries, summary=_summarize(values, entries))


def _summarize(values: Mapping[str, Any], entries: Sequence[TimelineEntry]) -> TimelineSummary:
    """The headline figures, derived from the same entries the view renders.

    The elapsed span is `max - min` over the readable stamps rather than
    last-minus-first: the entries are in execution order, and a checkpoint with
    one skewed stamp should not report a negative run duration.
    """
    stamps = sorted(dt for dt in (_instant(entry["timestamp"]) for entry in entries) if dt)
    duration = ""
    if len(stamps) > 1:
        duration = format_span((stamps[-1] - stamps[0]).total_seconds() * 1000)

    return TimelineSummary(
        started_at=entries[0]["timestamp"] if entries else "",
        ended_at=entries[-1]["timestamp"] if entries else "",
        duration=duration,
        attempts=sum(1 for entry in entries if entry["agent"] == "parser"),
        critic_rejections=sum(
            1 for entry in entries if entry["agent"] == "critic" and entry["status"] == "rejected"
        ),
        # The graph's own revision counter (#53): 0 for the Parser's extraction,
        # one up per reviewer revision. Authoritative, and it needs no correlation
        # with the log to be read.
        revisions=_int(values.get("criteria_revision")),
        escalated=any(entry["status"] == "escalated" for entry in entries),
        approved_by=_text(values.get("approved_by")),
        approved_by_role=_text(values.get("approved_by_role")),
        approved_at=_text(values.get("approved_at")),
    )
