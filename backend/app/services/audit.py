"""The org-wide index of human decisions (#98).

`services/timeline.py` answers *what happened in this run*, derived from the
run's own event log. The question an auditor asks first is the other one: *who
approved, rejected, revised or escalated anything, across every run, in this
window* — and no per-run view can answer it, because the answer spans runs that
were never opened together and reviewers who never worked the same protocol.

Five decisions worth knowing before editing this module:

**The index is written, not derived.** Every other view in this app is a
derivation over a checkpoint, and that is the right default — a derivation can be
corrected for every past run at once. It is the wrong default here. Answering
"every decision in July" by derivation means loading every checkpoint this
instance has ever written, and the cost grows with the history the endpoint
exists to search. So each decision is appended to `audit_events` at the moment it
is made (AC 4), and the query is a bounded, indexed read.

**The checkpoint stays the record; the index is a denormalization of it.** Which
is what settles the failure mode: a decision is written into the checkpoint
*first* and indexed after, and a failed index write is logged at ERROR with the
whole decision in the line rather than raised. Raising would fail an approval
that has already happened and is already durable — the worst of both, since the
client would retry a decision the graph has already taken. This is the same
ordering `reject_screening` documents for the store row.

**PHI-safe by construction, not by filtering.** An entry is eight fields
(`AuditDecision`), and none of them has anywhere for a patient to be: staff
identity, an action, a timestamp, the run and protocol it was about, a revision
number, and one sentence about the *protocol*. Nothing here reads
`matched_patients`, and the audit store cannot reach a checkpoint at all. That is
AC 5, and `tests/test_audit.py` asserts it against a run with a scored cohort.

**A reviewer sees their own decisions; an admin sees everyone's** (AC 7). Scoped
server-side in the SQL, by `visible_actor` below — never by filtering a full page
after the fact, and never by a parameter the client controls. An escalation is
the pipeline's act rather than a person's, so it carries `SYSTEM_ACTOR` and is
therefore admin-visible: it belongs to no reviewer, and attributing it to the one
who later picked it up would put a name on a decision they did not make.

**An export is complete or it is refused.** `MAX_EXPORT_ROWS` bounds what one
download serializes, and a filter matching more than that is a 413 naming the
count rather than a file quietly holding the newest ten thousand. A CSV has
nowhere to say it is short — a trailing note row would corrupt the parse the file
exists to feed — and an auditor who cannot tell a complete export from a partial
one holds the one artifact worse than no export at all. The JSON body still
carries `total` beside `exported` so the file is self-describing; the two are
equal by construction.
"""

from __future__ import annotations

import csv
import io
import json
import re
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, TypedDict

from app.exceptions import (
    AuditExportTooLargeError,
    AuthorizationDeniedError,
    InvalidAuditFilterError,
)
from app.logging_config import get_logger
from app.persistence import AuditDecision, AuditFilter, AuditRecord, AuditStore
from app.services.export import BOM, csv_cell

if TYPE_CHECKING:
    from app.auth import Principal

log = get_logger("audit")

# The decisions the index carries, in the order the filter offers them. Approval
# and rejection are the two answers to the gate (#50, #91), a revision is the
# third exit from it (#53), and an escalation is the pipeline handing a run back
# to a human (#55). Deliberately closed: a value outside this tuple is a 422 on
# the filter rather than a silently empty page.
ACTIONS = ("approved", "rejected", "criteria_revised", "escalated")

# Rendered names, the same convention `services/timeline.py` follows: an entry
# carries the label a human reads *and* the raw action, so the screen, the CSV and
# the JSON cannot name one decision three ways.
ACTION_LABELS = {
    "approved": "Approved",
    "rejected": "Rejected",
    "criteria_revised": "Criteria revised",
    "escalated": "Escalated",
}

# The actor behind a decision no person made. Not an email and not empty: an empty
# cell in an auditor's CSV reads as missing data, and anything email-shaped could
# collide with a configured account — `parse_users` requires an `@`, so this
# string can never be one.
SYSTEM_ACTOR = "trialgate-pipeline"
SYSTEM_ROLE = "system"

# The most rows one export carries. High enough to cover a real audit window (an
# instance doing fifty screenings a day takes most of a year to exceed it) and low
# enough that a single request cannot be made to assemble an unbounded string in
# memory. A filter matching more is refused — see the module docstring.
MAX_EXPORT_ROWS = 10_000

# A `from`/`to` filter given as a bare calendar day rather than an instant.
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# The CSV's columns, in order. `id` leads because it is the one stable handle on a
# row — an auditor quoting a line back is quoting this — and the rendered `label`
# rides beside the raw `action` for the same reason the API payload carries both.
_CSV_COLUMNS = (
    "id",
    "occurred_at",
    "action",
    "action_label",
    "actor",
    "actor_role",
    "thread_id",
    "source_filename",
    "criteria_revision",
    "detail",
)


class AuditEntry(TypedDict):
    """One decision, ready to render.

    `label` is `action` in the form a human reads; both travel, so a machine
    consumer filters on the enum while a screen prints the name (the convention
    `TimelineEntry` established). `revision` is non-zero only for a
    `criteria_revised` entry, and it is what lets a client deep-link to that
    revision's before/after diff on the run rather than to the run at large (AC 3).
    """

    id: int
    thread_id: str
    action: str
    label: str
    actor: str
    actor_role: str
    occurred_at: str
    detail: str
    revision: int
    source_filename: str


# --- Recording --------------------------------------------------------------


async def record(
    store: AuditStore,
    action: str,
    thread_id: str,
    *,
    actor: Principal | None,
    detail: str,
    revision: int = 0,
    source_filename: str = "",
    occurred_at: str | None = None,
) -> None:
    """Append one decision to the index, as it happens.

    `actor` is None for a decision the pipeline made rather than a person — see
    `SYSTEM_ACTOR`. `occurred_at` should be the stamp already written into the
    checkpoint where there is one, so the index and the run's own trail agree to
    the microsecond rather than to within a round trip; it defaults to now for a
    decision that carries no such stamp.

    Never raises. See the module docstring: the decision is already durable in the
    checkpoint by the time this runs, and turning an indexing failure into a failed
    approval would ask the client to retry something the graph has already done.
    The ERROR line carries every field, so the entry is recoverable from logs.
    """
    decision = AuditDecision(
        thread_id=thread_id,
        action=action,
        actor=actor.email if actor else SYSTEM_ACTOR,
        actor_role=actor.role if actor else SYSTEM_ROLE,
        occurred_at=occurred_at or datetime.now(UTC).isoformat(),
        detail=detail,
        revision=revision,
        source_filename=source_filename,
    )
    try:
        await store.record(decision)
    except Exception:  # noqa: BLE001 — an index write must not undo a made decision
        log.error("audit.record_failed", exc_info=True, **vars(decision))


# --- Reading ----------------------------------------------------------------


def visible_actor(principal: Principal, requested: str | None) -> str | None:
    """The actor filter to apply for this caller, enforcing AC 7.

    An admin reads the whole index and may narrow it to anyone. A reviewer is
    scoped to themselves: with no `actor` asked for they get their own decisions,
    and asking for somebody else's is a 403 rather than an empty page — the
    distinction between "this person did nothing" and "you may not ask" is exactly
    what an empty result would blur.

    Returned as the value the query filters on (None meaning "no narrowing"), so
    the scope is applied in the SQL rather than by trimming a page that has already
    been read.
    """
    wanted = (requested or "").strip().lower() or None
    if principal.role == "admin":
        return wanted
    if wanted and wanted != principal.email.lower():
        raise AuthorizationDeniedError(
            "A reviewer can only read their own decisions; ask an admin for anyone else's."
        )
    return principal.email.lower()


def parse_bound(value: str, *, upper: bool) -> str:
    """A caller's date-range filter as a string comparable with `occurred_at`.

    Accepts a bare calendar day (`2026-08-11`) or a full ISO-8601 instant. A bare
    day covers the *whole* day — 00:00:00 for `from`, 23:59:59.999999 for `to` —
    because a reader who types one date means that date, and an upper bound at
    midnight would silently exclude every decision made during it. A naive instant
    is read as UTC, and everything is normalized to UTC, which is the offset every
    stored stamp is written in (see `AuditFilter`).

    A bare day is a *UTC* day, which is the only thing a bare day can mean to a
    server that is not told the caller's zone. The app's own filter therefore sends
    instants rather than days (see `frontend/src/lib/audit.dayBound`): its table
    renders each stamp in the reader's timezone, so a bare day would disagree with
    the rows on screen for anyone not on UTC. A script asking for a UTC day gets
    exactly that.

    Anything unparseable is a 422: a filter this endpoint could not honor must be
    refused, never dropped, or the page returned is wider than the one asked for.
    """
    text = value.strip()
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise InvalidAuditFilterError(
            f"{text!r} is not a date this filter understands — use YYYY-MM-DD or a full "
            "ISO-8601 timestamp."
        ) from exc
    if upper and _DATE_ONLY.match(text):
        moment = moment.replace(hour=23, minute=59, second=59, microsecond=999999)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat()


def build_filter(
    *,
    actor: str | None,
    action: str | None,
    thread_id: str | None,
    since: str | None,
    until: str | None,
) -> AuditFilter:
    """Validate and normalize the query string into an `AuditFilter`.

    The action is checked against `ACTIONS` here rather than being declared as a
    `Literal` on the route, because the same validation has to cover the export
    route and this keeps one message for both. An inverted range (`from` after
    `to`) is a 422 too — it can only ever match nothing, and reporting that as an
    empty index invites the reader to conclude nobody did anything.
    """
    if action and action not in ACTIONS:
        raise InvalidAuditFilterError(
            f"Unknown action {action!r}; expected one of {', '.join(ACTIONS)}."
        )
    lower = parse_bound(since, upper=False) if since else None
    upper = parse_bound(until, upper=True) if until else None
    if lower and upper and lower > upper:
        raise InvalidAuditFilterError("The start of the date range is after its end.")
    return AuditFilter(
        actor=actor,
        action=action,
        thread_id=thread_id,
        since=lower,
        until=upper,
    )


def entry(row: AuditRecord) -> AuditEntry:
    """One stored row as the payload every reader of this index gets."""
    return AuditEntry(
        id=row.id,
        thread_id=row.thread_id,
        action=row.action,
        label=ACTION_LABELS.get(row.action, row.action),
        actor=row.actor,
        actor_role=row.actor_role,
        occurred_at=row.occurred_at,
        detail=row.detail,
        revision=row.revision,
        source_filename=row.source_filename,
    )


async def list_decisions(
    store: AuditStore,
    principal: Principal,
    *,
    limit: int,
    offset: int,
    actor: str | None = None,
    action: str | None = None,
    thread_id: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> dict[str, Any]:
    """One page of the index, newest first (AC 1, AC 2).

    Returns `{items, total, limit, offset, scope}` — the same envelope
    `list_screenings` uses, for the same reason: a bare list cannot say whether
    more rows matched than were returned. `scope` echoes the filter that was
    actually applied, which is how a reviewer's client can tell that the page it is
    showing is their own decisions rather than the whole org's without inferring it
    from the role.
    """
    criteria = build_filter(
        actor=visible_actor(principal, actor),
        action=action,
        thread_id=thread_id,
        since=since,
        until=until,
    )
    page = await store.list(limit=limit, offset=offset, filters=criteria)
    return {
        "items": [entry(row) for row in page.items],
        "total": page.total,
        "limit": limit,
        "offset": offset,
        "scope": _scope(criteria),
    }


def _scope(criteria: AuditFilter) -> dict[str, Any]:
    """The filter as it was applied, for the response envelope and the export."""
    return {
        "actor": criteria.actor,
        "action": criteria.action,
        "thread_id": criteria.thread_id,
        "from": criteria.since,
        "to": criteria.until,
    }


# --- Export (AC 6) ----------------------------------------------------------


async def export_decisions(
    store: AuditStore,
    principal: Principal,
    fmt: str,
    *,
    actor: str | None = None,
    action: str | None = None,
    thread_id: str | None = None,
    since: str | None = None,
    until: str | None = None,
    generated_at: datetime | None = None,
) -> tuple[str, str]:
    """The filtered index as a file for an external auditor: `(filename, body)`.

    Takes the same filters as the index and applies the same scope, so what an
    auditor downloads is exactly the page they were looking at rather than a
    second, wider query — a reviewer must not be able to export what they cannot
    read.

    The cohort export's shape (#102), deliberately: CSV for a spreadsheet, JSON for
    a machine, one function returning the body and the name it should be saved
    under while the route owns the attachment mechanics.

    A result over `MAX_EXPORT_ROWS` is **refused**, not truncated — see the module
    docstring. A CSV has nowhere to state that it is short of the whole answer (a
    trailing note row would corrupt the parse it exists to feed), and an auditor
    who cannot tell a complete export from a partial one has the one artifact that
    is worse than no export at all.
    """
    criteria = build_filter(
        actor=visible_actor(principal, actor),
        action=action,
        thread_id=thread_id,
        since=since,
        until=until,
    )
    # One row over the cap, so a result at exactly the cap exports and the first
    # one past it is caught — without a second COUNT query to find out which.
    page = await store.list(limit=MAX_EXPORT_ROWS + 1, offset=0, filters=criteria)
    if page.total > MAX_EXPORT_ROWS:
        raise AuditExportTooLargeError(
            f"This filter matches {page.total} decisions; one export carries at most "
            f"{MAX_EXPORT_ROWS}. Narrow the date range and take the log in parts."
        )
    entries = [entry(row) for row in page.items]
    stamped = (generated_at or datetime.now(UTC)).astimezone(UTC)

    filename = f"trialgate-audit-{stamped.date().isoformat()}.{fmt}"
    if fmt == "json":
        return filename, _json_body(entries, page.total, criteria, stamped)
    return filename, render_csv(entries)


def _json_body(
    entries: Sequence[AuditEntry], total: int, criteria: AuditFilter, stamped: datetime
) -> str:
    """The JSON download's whole body — the filter, the counts, then the rows."""
    return json.dumps(
        {
            "generated_at": stamped.isoformat(),
            "scope": _scope(criteria),
            # Both figures, always. `exported` under `total` is the only way the
            # file itself can say it was truncated — see the module docstring.
            "total": total,
            "exported": len(entries),
            "decisions": list(entries),
        },
        indent=2,
        ensure_ascii=False,
    )


def render_csv(entries: Iterable[AuditEntry]) -> str:
    """The index as a spreadsheet: one row per decision, in `_CSV_COLUMNS` order.

    Shares `csv_cell` and the BOM with the cohort export (#102) rather than
    re-deriving them: `detail` carries a rejection reason a reviewer typed and
    `source_filename` came off an upload, so this file is exactly as untrusted as
    that one, and a formula-injection mitigation that lived in two places would
    eventually be fixed in one.
    """
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(_CSV_COLUMNS)
    for item in entries:
        writer.writerow(
            [
                item["id"],
                item["occurred_at"],
                item["action"],
                item["label"],
                csv_cell(item["actor"]),
                item["actor_role"],
                item["thread_id"],
                csv_cell(item["source_filename"]),
                item["revision"],
                csv_cell(item["detail"]),
            ]
        )
    return BOM + buffer.getvalue()
