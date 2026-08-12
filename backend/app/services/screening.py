"""Screening use-cases — create, stream, approve, inspect — orchestrating the
store and the graph so route handlers stay thin HTTP translators.

Routes hand this layer raw inputs (upload bytes, a thread_id) and the wired
dependencies (`store`, `graph`); it owns everything in between — input parsing,
state construction, graph invocation, status denormalization, gate/escalation
notification, and SSE framing.
The graph is assembled here (`build_screening_graph`) so `app/main.py` never
imports the graph builder directly.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol
from uuid import uuid4

from fastapi.encoders import jsonable_encoder

from app.auth import Principal
from app.config import get_settings
from app.exceptions import (
    CriteriaRevisionConflictError,
    ExtractionError,
    InvalidComparisonError,
    PayloadTooLargeError,
    ScreenerError,
    ScreeningNotApprovableError,
    ScreeningNotEditableError,
    ScreeningNotFoundError,
    ScreeningNotRejectableError,
    ScreeningNotReportableError,
    UnsupportedMediaTypeError,
)
from app.graph.builder import build_graph
from app.graph.state import ScreeningStatus, event, initial_state
from app.logging_config import bind_contextvars, get_logger
from app.services import (
    attrition,
    cohort,
    comparison,
    coverage,
    criteria_edits,
    metrics,
    metrics_summary,
    notifications,
    provenance,
    report,
    simulation,
    sse,
    timeline,
    usage,
)
from app.services.pdf import extract_eligibility_text
from app.services.uploads import (
    UploadedFile,
    read_upload_capped,
    sanitize_filename,
    validate_content_type,
)

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.graph.state import CompiledStateGraph

    from app.persistence import ScreeningStore

log = get_logger("screening")

# Re-exported so `app.main` can type the list endpoint's status filter without
# importing the graph package (routes stay one layer away from it, as they do
# for the builder).
__all__ = [
    "ScreeningStatus",
    "approve_screening",
    "build_screening_graph",
    "compare_screenings",
    "create_screening",
    "create_screening_batch",
    "get_metrics_summary",
    "get_screening_protocol",
    "get_screening_report",
    "get_screening_state",
    "list_screenings",
    "reject_screening",
    "resume_with_edited_criteria",
    "simulate_screening",
    "stream_screening",
]

# How many of the most recent runs the coverage aggregate (#93) reads. Unlike
# every other figure in the metrics summary, that one cannot come off a Prometheus
# collector: ranking the `unparseable` phrasings needs the sentences themselves,
# which live in the checkpoints. So it is a bounded walk — one checkpoint read per
# run — and the payload states both this window and the instance's total, because a
# sample that read as the whole history would be the one way this could mislead.
COVERAGE_SAMPLE = 50


class Snapshot(Protocol):
    """The slice of a LangGraph state snapshot this layer reads."""

    next: tuple[str, ...]
    values: dict[str, Any]


class ScreeningGraph(Protocol):
    """The three graph capabilities the service drives — depending on this
    interface (not the concrete compiled graph) keeps the layer test-double
    friendly and independent of LangGraph internals. ``aget_state`` returns
    ``Any`` so the concrete ``CompiledStateGraph`` (whose ``StateSnapshot`` is a
    structural ``Snapshot``) satisfies the protocol without an invariance clash."""

    def astream(
        self, input: Any, config: Any = ..., *, stream_mode: Any = ...
    ) -> AsyncIterator[Any]: ...

    # Yields node-update dicts for a single stream_mode, or (mode, chunk) tuples
    # when stream_mode is a list (approve uses ["updates", "custom"]) — hence Any.

    async def aget_state(self, config: Any) -> Any: ...

    # `as_node` rewrites the checkpoint as if that node had just produced these
    # values, which is what moves the graph's cursor (edit-and-rerun uses it to
    # rewind to the parser). Omitted — the approve path — the values merge into the
    # current checkpoint and the thread stays parked where it was.
    async def aupdate_state(
        self, config: Any, values: dict[str, Any], as_node: str | None = ...
    ) -> Any: ...

    async def ainvoke(self, input: Any, config: Any = ...) -> dict[str, Any]: ...


def build_screening_graph(checkpointer: BaseCheckpointSaver) -> CompiledStateGraph:
    """The graph the service drives; assembled here so routes never import the builder."""
    return build_graph(checkpointer)


async def _require_thread(store: ScreeningStore, thread_id: str) -> RunnableConfig:
    if not await store.exists(thread_id):
        raise ScreeningNotFoundError(f"No screening found for thread_id {thread_id}")
    return {"configurable": {"thread_id": thread_id}}


def _status_from_snapshot(snapshot: Snapshot) -> str:
    """Coarse, list-friendly status denormalized from the graph's own state.

    Kept in the store so `GET /api/screenings` never has to load every
    checkpoint just to render a status column.
    """
    if snapshot.next:
        return "awaiting_approval"
    step = snapshot.values.get("current_step")
    return str(step) if step else "done"


def _criteria_count(values: dict[str, Any]) -> int:
    """How many criteria the parser extracted, across all four buckets.

    `services/coverage.py` owns the count because it is the numerator of that
    module's parse layer (#93): the runs index column and the coverage panel are
    the same number, so there is one definition of what "criteria found" means.
    """
    return coverage.structured_count(values)


def _match_count(values: dict[str, Any]) -> int:
    """How many patients the run actually matched — the eligible bucket alone.

    The bucketing rule lives in services/cohort.py, shared with the exported
    report and the run comparison (#59), so this column and those views can't
    disagree about who was eligible.
    """
    return cohort.matched_count(values.get("matched_patients") or [])


def _summary_columns(values: dict[str, Any]) -> dict[str, int]:
    """Everything the runs index renders for a row, derived from one snapshot.

    Both writers of these columns go through here, so a rejected run's row and a
    finished run's row are filled in by one derivation. `criteria_count` is the
    coverage payload's own `structured` figure rather than a second count of the
    same four buckets — the index's "Criteria" column and its "Coverage" column
    have to be two readings of one extraction (#93).

    The LLM bill (#101) is denormalized the same way and from the same snapshot,
    so the cost a row reports and the cost its detail view breaks down by node
    are one reduction of one checkpoint.
    """
    screenability = coverage.build_coverage(values)
    bill = usage.build_usage(values)
    return {
        "criteria_count": screenability["structured"],
        "match_count": _match_count(values),
        "coverage_checkable": screenability["checkable"],
        "coverage_criteria": screenability["criteria"],
        "llm_tokens": bill["tokens"],
        "llm_cost_micro_usd": bill["cost_micro_usd"],
    }


async def _record_outcome(store: ScreeningStore, thread_id: str, snapshot: Snapshot) -> str:
    """Denormalize the finished (or parked) run into the store's summary columns,
    returning the status it recorded.

    Called once per terminal frame, so the runs index (#51) can render status,
    counts and coverage for every row without loading a checkpoint per screening.
    The status is handed back rather than recomputed by the caller, so the row the
    index shows and the notification a reviewer gets (#60) can never disagree
    about the phase.
    """
    status = _status_from_snapshot(snapshot)
    await store.set_status(thread_id, status, **_summary_columns(snapshot.values))
    return status


async def _notify_if_parked(thread_id: str, status: str, snapshot: Snapshot) -> None:
    """Push a notification when the run stopped somewhere a human has to act (#60).

    Dispatched here — after the outcome is recorded, before the terminal frame is
    yielded — so it runs on the run's own task rather than a detached one. A
    fire-and-forget task can be cancelled by a shutdown mid-flight, and moving
    this after the final yield would skip it precisely when the client has already
    disconnected, which is the departed reviewer this feature exists to reach.

    The cost is bounded by `NOTIFY_TIMEOUT_SECONDS` and paid only by deployments
    that opted in: `notify_gate` returns immediately when notifications are off or
    the status isn't one a person has to act on, and never raises — a webhook
    outage must not turn a successful screening into an `__error__` frame.
    """
    await notifications.notify_gate(
        get_settings(),
        thread_id=thread_id,
        status=status,
        source_filename=snapshot.values.get("source_filename"),
        criteria_count=_criteria_count(snapshot.values),
    )


def _terminal_frame(snapshot: Snapshot) -> str:
    """Translate the graph's final state into a terminal SSE frame.

    A node that absorbed a failure (parser LLM-outage / unrepairable output)
    ends the run with current_step="failed" *without* raising, so it reaches
    here rather than the except blocks below. Surface it as __error__ so the
    UI shows a real failure instead of a silently-successful empty screening.
    """
    if snapshot.next:
        return sse.interrupt_frame()
    if snapshot.values.get("current_step") == "failed":
        events = snapshot.values.get("events") or []
        message = events[-1]["detail"] if events else "Screening failed."
        return sse.error_frame(message)
    return sse.end_frame()


async def _graph_frames(
    store: ScreeningStore,
    graph: ScreeningGraph,
    thread_id: str,
    config: RunnableConfig,
    graph_input: Any,
    *,
    stream_mode: Any,
    operation: str,
) -> AsyncIterator[str]:
    """Drive the graph and render its progress as SSE frames.

    The shared body of every streaming route (initial run, approve, edit-and-rerun
    — `operation` names which, for the log line). They differ only in what they
    feed the graph and which stream modes they ask for; the framing, the terminal
    frame, the outcome denormalization and — critically — the error contract are
    identical, and having one copy is what keeps them that way.

    An exception here can't become an HTTP status: the response headers are
    already sent by the time the first frame is yielded. The terminal `__error__`
    frame is the only error channel left, so this must catch *everything* or the
    frontend waits forever on a stream that will never end.

    `stream_mode` as a list makes LangGraph yield `(mode, chunk)` tuples; a
    single mode yields bare chunks, and the fakes in the test suite always do —
    hence the isinstance check rather than a per-caller branch.
    """
    try:
        async for item in graph.astream(graph_input, config, stream_mode=stream_mode):
            mode, chunk = item if isinstance(item, tuple) else ("updates", item)
            if mode == "custom":
                yield sse.progress_frame(jsonable_encoder(chunk))
                continue
            for node, update in chunk.items():
                yield sse.update_frame(node, jsonable_encoder(update))
        snapshot = await graph.aget_state(config)
        status = await _record_outcome(store, thread_id, snapshot)
        await _notify_if_parked(thread_id, status, snapshot)
        yield _terminal_frame(snapshot)
        log.info(f"screening.{operation}_finished")
    except ScreenerError as exc:
        log.warning(f"screening.{operation}_error", error=type(exc).__name__, detail=str(exc))
        await store.set_status(thread_id, "failed")
        yield sse.error_frame(str(exc))
    except Exception:  # noqa: BLE001 — last-resort terminator, detail stays server-side
        log.error(f"screening.{operation}_crashed", exc_info=True)
        await store.set_status(thread_id, "failed")
        yield sse.error_frame("Screening failed unexpectedly — check server logs.")


async def create_screening(
    store: ScreeningStore,
    filename: str | None,
    raw: bytes,
    content_type: str | None = None,
    *,
    max_pdf_pages: int | None = None,
    max_text_chars: int | None = None,
) -> str:
    """Parse the upload into eligibility text, persist it, and return its thread_id.

    The filename is sanitized to a traversal-free basename *before* it is stored
    or logged — the raw name is attacker-controlled and only trusted to detect a
    PDF (by extension or content type) so its bytes go through PyMuPDF, which
    validates them and raises ExtractionError (422) on a non-PDF.

    PDF parsing is CPU-bound and offloaded to a thread so a large document can't
    stall the event loop for every other in-flight request. The extracted text is
    truncated to `max_text_chars` so the downstream LLM prompts are bounded
    regardless of upload size.
    """
    is_pdf = (filename or "").lower().endswith(".pdf") or (content_type or "").lower() == (
        "application/pdf"
    )
    if is_pdf:
        text = await asyncio.to_thread(extract_eligibility_text, raw, max_pdf_pages)
    else:
        text = raw.decode("utf-8", errors="replace")
    if max_text_chars is not None:
        text = text[:max_text_chars]

    safe_filename = sanitize_filename(filename)
    thread_id = str(uuid4())
    bind_contextvars(thread_id=thread_id)
    # Persist the input durably so a restart between upload and stream — or a
    # second worker handling the stream — loses nothing. The graph's execution
    # state is rebuilt from initial_state() when the run first streams.
    await store.create(thread_id, safe_filename, text)
    # PHI hygiene: log the size of the upload, never its contents.
    log.info("screening.created", source_filename=safe_filename, text_chars=len(text))
    return thread_id


# The per-file rejections a batch reports without failing: "this document is not
# one we can screen". Everything else a create can raise — a store outage
# (DataStoreError), an unexpected crash — is about the server rather than the
# file, so it propagates and fails the whole submission instead of being reported
# ten times as ten bad protocols.
_BATCH_ITEM_ERRORS = (UnsupportedMediaTypeError, PayloadTooLargeError, ExtractionError)


async def create_screening_batch(
    store: ScreeningStore,
    files: Sequence[UploadedFile],
    *,
    allowed_content_types: frozenset[str],
    max_upload_bytes: int,
    max_pdf_pages: int | None = None,
    max_text_chars: int | None = None,
) -> dict:
    """Turn several uploaded protocols into several screenings, one submission (#61).

    Each file becomes its own thread — the same `create_screening` the single
    upload route calls, so a batched run is indistinguishable from an individually
    uploaded one everywhere downstream: the runs index, the review queue, the
    report. There is deliberately no batch entity in the store; grouping N runs
    under a batch id would invent a second thing to navigate, and what a
    coordinator needs is the N runs.

    **Partial success is the contract.** A batch of eight protocols where the
    third is a scanned PDF with no extractable text must still screen the other
    seven, so the three per-file rejections (`_BATCH_ITEM_ERRORS`) are reported
    per item and the response is a 200 either way. `items` echoes the submission's
    order, one entry per file, each carrying `thread_id` or `{error, detail}` —
    the same error/detail pair the HTTP contract uses, so a client renders a
    rejected file exactly as it renders a failed single upload.

    Files are processed one at a time rather than concurrently: PDF extraction is
    CPU-bound (offloaded per file to a thread), and ten of them at once would put
    the event loop's threadpool under a spike that every other in-flight request
    pays for. The wait is the reviewer's own upload, and they are the one who
    asked for ten.

    Nothing is *run* here. Like a single upload, each screening only executes when
    a client streams it (`GET /api/screenings/{id}/stream`), which is what keeps
    the concurrency gate — not the size of someone's file picker — in charge of how
    many graph runs are in flight.
    """
    items: list[dict[str, Any]] = []
    for file in files:
        # Sanitized up front so the echoed name is safe even on the paths that
        # never reach the store (a 415 rejection still names the file back).
        safe_filename = sanitize_filename(file.filename)
        try:
            validate_content_type(file.content_type, file.filename, allowed_content_types)
            raw = await read_upload_capped(file, max_upload_bytes)
            thread_id = await create_screening(
                store,
                file.filename,
                raw,
                content_type=file.content_type,
                max_pdf_pages=max_pdf_pages,
                max_text_chars=max_text_chars,
            )
        except _BATCH_ITEM_ERRORS as exc:
            log.warning(
                "screening.batch_item_rejected",
                source_filename=safe_filename,
                error=type(exc).__name__,
                detail=str(exc),
            )
            items.append(
                {
                    "filename": safe_filename,
                    "thread_id": None,
                    "error": type(exc).__name__,
                    "detail": str(exc),
                }
            )
            continue
        items.append(
            {"filename": safe_filename, "thread_id": thread_id, "error": None, "detail": None}
        )

    created = sum(1 for item in items if item["thread_id"])
    rejected = len(items) - created
    # One line per submission, with the split: each rejection also logs its own
    # warning above, but this is what says whether a batch was mostly refused.
    log.info("screening.batch_created", files=len(items), created=created, rejected=rejected)
    return {"items": items, "created": created, "rejected": rejected}


async def list_screenings(
    store: ScreeningStore,
    *,
    limit: int,
    offset: int,
    status: ScreeningStatus | None = None,
    search: str | None = None,
) -> dict:
    """One page of screenings, newest first — backs the runs index (#51).

    Returns an envelope rather than a bare list because the page alone can't
    answer "is there a next one": `total` is the count matching the filter, not
    the count on this page.

    `coverage` is the screenability score (#93) rebuilt from the row's two
    denormalized columns — read here rather than derived from a checkpoint,
    because a page view would otherwise load one per row. The percentage is
    resolved by `coverage.score_of` rather than divided in the browser, so a row
    in this list and the panel on the run's own page are one formula read twice.
    """
    page = await store.list(limit=limit, offset=offset, status=status, search=search)
    return {
        "items": [
            {
                "thread_id": r.thread_id,
                "source_filename": r.source_filename,
                "status": r.status,
                "created_at": r.created_at,
                "criteria_count": r.criteria_count,
                "match_count": r.match_count,
                "coverage": coverage.CoverageSummary(
                    checkable=r.coverage_checkable,
                    criteria=r.coverage_criteria,
                    score=coverage.score_of(r.coverage_checkable, r.coverage_criteria),
                ),
                # The run's LLM bill (#101), rebuilt from the row's two
                # denormalized columns for the same reason coverage is — a page
                # view must not load a checkpoint per row. Dollars come from
                # `usage.usd`, the one conversion out of micro-USD, so a row here
                # and the panel on the run's own page are one formula read twice.
                "llm_tokens": r.llm_tokens,
                "llm_cost_usd": usage.usd(r.llm_cost_micro_usd),
            }
            for r in page.items
        ],
        "total": page.total,
        "limit": limit,
        "offset": offset,
    }


async def get_metrics_summary(
    store: ScreeningStore, graph: ScreeningGraph, *, sample: int = COVERAGE_SAMPLE
) -> dict:
    """The in-app metrics summary (#58) with the coverage aggregate beside it (#93).

    Composed here rather than in `services/metrics_summary.py` because the two
    halves come from different places on purpose: the funnel, the Critic
    rejections and the loop depth are read off the live Prometheus collectors and
    need no store at all, while coverage is a reduction over checkpoints — and a
    ranking of `unparseable` phrasings is not something a Prometheus label can
    carry (a protocol sentence per label value is unbounded cardinality, and the
    counter would answer "how many" without ever saying "of what").

    So this walks the `sample` most recent runs, one checkpoint read each, and
    pools their coverage. That is the cost of the vocabulary backlog being
    data-driven, and it is bounded and stated: the payload carries how many runs
    were read and how many exist.

    The reads are sequential rather than gathered: they are one connection's
    worth of cheap single-row lookups, and holding fifty `aget_state` calls in
    flight for a read-only page would trade a page nobody waits on for contention
    every other request pays for. The values go to `build_coverage` raw rather than
    through `jsonable_encoder`: it reads two fields, and re-encoding fifty whole
    checkpoints — protocol text, event log, entire cohorts — to read them would be
    the most expensive thing on this request by an order of magnitude.

    One unreadable checkpoint costs its own run and nothing else. Before this block
    existed the endpoint could not fail at all (three `collect()` calls over
    in-memory counters), and a single corrupt row taking the funnel down with it
    would be a bad trade for a figure that is a sample anyway. A skipped run is
    logged and drops out of `sampled`, so the payload's own window narrows rather
    than the page quietly claiming it read everything.
    """
    page = await store.list(limit=sample, offset=0)
    coverages = []
    for record in page.items:
        try:
            snapshot = await graph.aget_state({"configurable": {"thread_id": record.thread_id}})
        except Exception:  # noqa: BLE001 — one bad checkpoint must not fail the page
            log.warning(
                "screening.coverage_read_failed", failed_thread=record.thread_id, exc_info=True
            )
            continue
        coverages.append(coverage.build_coverage(snapshot.values))
    aggregate = coverage.aggregate(coverages, total=page.total)
    log.info("screening.coverage_aggregated", runs=aggregate["runs"], score=aggregate["score"])
    return {**metrics_summary.summarize_metrics(), "coverage": aggregate}


async def stream_screening(
    store: ScreeningStore, graph: ScreeningGraph, thread_id: str
) -> AsyncIterator[str]:
    """Validate the thread, then return an SSE frame iterator for the graph run.

    Validation happens eagerly (raising ScreeningNotFoundError before any frame
    is yielded) so an unknown thread_id becomes a 404 HTTP response, not an
    error buried mid-stream after the response headers are already sent.
    """
    config = await _require_thread(store, thread_id)
    bind_contextvars(thread_id=thread_id)
    screening_input = await store.get_input(thread_id)
    assert screening_input is not None  # exists() just passed
    log.info("screening.stream_started")
    state = initial_state(screening_input.raw_protocol_text, screening_input.source_filename)
    return _graph_frames(
        store, graph, thread_id, config, state, stream_mode="updates", operation="stream"
    )


async def _record_approver(
    graph: ScreeningGraph, config: RunnableConfig, approver: Principal
) -> None:
    """Stamp the approver into the parked checkpoint before it resumes (#50).

    `aupdate_state` merges values into the *current* checkpoint without moving
    the cursor: the thread stays parked with `next == ("matcher",)`, so the
    `astream(None, ...)` resume below still enters the matcher — which now sees
    `approved_by` in its state. `events` carries an append reducer, so the audit
    entry joins the same log the frontend renders rather than replacing it.
    """
    await graph.aupdate_state(
        config,
        {
            "approved_by": approver.email,
            "approved_by_role": approver.role,
            "approved_at": datetime.now(UTC).isoformat(),
            "events": [
                event(
                    "human",
                    "approved",
                    f"Approval gate cleared by {approver.email} ({approver.role})",
                )
            ],
        },
    )


async def approve_screening(
    store: ScreeningStore, graph: ScreeningGraph, thread_id: str, approver: Principal
) -> AsyncIterator[str]:
    """Resume past the human-in-the-loop gate and STREAM the matcher over SSE.

    The matcher makes LLM calls over the whole cohort and can run for minutes on
    a local model. Streaming it — instead of blocking the POST until it returns —
    keeps approval responsive (a slow model no longer times out the client) and
    reuses the exact frame/error contract `stream_screening` uses: the matcher's
    node update carries `matched_patients`, then a terminal frame closes the run.

    Validation is eager (raising before any frame is yielded) so an unknown
    thread or a screening not at the gate becomes an HTTP error, not a frame
    buried after the response headers are already sent. It checks for the
    *matcher* specifically, not merely for something pending: since #53 a thread
    can also be parked before the Critic (an edit-and-rerun whose client vanished
    between the checkpoint write and the resume), and `approved_by` has to keep
    meaning "authorized patient matching" — not "happened to POST while the run was
    parked somewhere". An already-finished screening has nothing pending and is
    rejected by the same check.

    `approver` is who authorized touching patient data (#50). It is written into
    the checkpoint *before* the resume, so the audit trail records the
    authorization even if the matcher subsequently fails — and the matcher itself
    can read it out of state.
    """
    config = await _require_thread(store, thread_id)
    bind_contextvars(thread_id=thread_id)
    if "matcher" not in (await graph.aget_state(config)).next:
        raise ScreeningNotApprovableError("screening is not awaiting approval")
    await _record_approver(graph, config, approver)
    # Server-side counterpart to the in-state event: who cleared the gate, in the
    # same correlated log stream as the rest of the run.
    log.info("screening.approved", approved_by=approver.email, approver_role=approver.role)
    # None input resumes from the interrupt_before=["matcher"] checkpoint. Two
    # stream modes: "updates" carries the matcher's terminal node result; "custom"
    # carries its mid-flight progress (see the matcher's _progress_emitter) so the
    # stream emits real frames during the long LLM matching pass, keeping the
    # idle-timeout reaper from killing a working run.
    return _graph_frames(
        store,
        graph,
        thread_id,
        config,
        None,
        stream_mode=["updates", "custom"],
        operation="approve",
    )


async def reject_screening(
    store: ScreeningStore,
    graph: ScreeningGraph,
    thread_id: str,
    reviewer: Principal,
    reason: str,
) -> dict:
    """Stop a screening at the human gate, on the record (#91).

    The gate's other answer, and the one it was missing: approval says "this
    extraction is good enough to score patients against", and this says "this
    protocol is not screenable — stop". Before it existed, a reviewer who reached
    that conclusion could only walk away, leaving the run parked in
    `awaiting_approval` forever and counted as in flight by the funnel.

    Symmetric with approval in the ways that matter. The identity and the reason
    are written into the checkpoint *before* the graph terminates, so the decision
    is durable even if everything after it fails; the reason is required, because
    a terminal state with no explanation is a dead end an auditor cannot read; and
    the same `human` event log carries it, so the run timeline shows the rejection
    beside the steps that led to it.

    Asymmetric in one way, deliberately: this does not stream. Approval resumes
    the matcher, which is minutes of LLM work; a rejection runs nothing at all, so
    it is a plain JSON response and holds no concurrency slot.

    **Which runs can be rejected.** Parked at the gate, or escalated after the
    Critic gave up — the two states where a human owns the run. Everything else is
    a 409: a finished run already produced a cohort someone approved, a failed one
    already stopped for a reason of its own, and a run still executing has not
    asked anyone anything yet. A run parked *before* the Critic (an edit-and-rerun
    whose client vanished, #53) is refused for the same reason approval refuses
    it — the gate it is waiting at is not this one.

    The checkpoint write is what terminates the graph. `as_node="matcher"` moves
    the cursor past the interrupt as though the matcher had run and produced these
    values, so `next` empties and the thread stops being pending — the matcher
    itself never executes, and no patient data is touched. An escalated run has
    already reached END, so it needs no cursor move and gets a plain merge.

    The checkpoint is written before the store row, and that order is deliberate:
    the checkpoint is the record, the row is denormalized from it. A store outage
    in between costs a stale status column on the runs index — recoverable, and
    visibly wrong — rather than a row claiming a decision the audit trail never
    received.
    """
    config = await _require_thread(store, thread_id)
    bind_contextvars(thread_id=thread_id)
    snapshot = await graph.aget_state(config)
    at_gate = "matcher" in snapshot.next
    escalated = not snapshot.next and snapshot.values.get("current_step") == "escalated"
    if not (at_gate or escalated):
        raise ScreeningNotRejectableError(
            "Only a screening parked at the approval gate or escalated for human review can be "
            "rejected."
        )

    rejected_at = datetime.now(UTC).isoformat()
    await graph.aupdate_state(
        config,
        {
            "rejected_by": reviewer.email,
            "rejected_by_role": reviewer.role,
            "rejected_at": rejected_at,
            "rejected_reason": reason,
            "current_step": "rejected",
            "events": [
                event(
                    "human",
                    "rejected",
                    f"Screening rejected by {reviewer.email} ({reviewer.role}) — {reason}",
                )
            ],
        },
        as_node="matcher" if at_gate else None,
    )
    # Written explicitly rather than denormalized from a re-read snapshot: the
    # status of a rejected run is a decision that was just made, not an
    # observation about where the graph stopped, and `_status_from_snapshot`
    # would quietly report "awaiting_approval" again if the cursor move ever
    # regressed — hiding the exact bug this feature exists to fix. The counts come
    # from the pre-rejection snapshot, so the runs index keeps showing what the
    # run had extracted (`match_count` is 0; the matcher never ran) and what share
    # of it was checkable — often the very reason the run was rejected (#93).
    await store.set_status(thread_id, "rejected", **_summary_columns(snapshot.values))
    # Counted only now — after the checkpoint and the store agree — so the funnel
    # can never report a rejection that isn't on the record.
    metrics.record_rejection(snapshot.values)
    # Server-side counterpart to the in-state event, in the same correlated stream
    # as the approval it replaces. The reason itself is in the checkpoint and the
    # event log; only its size is logged, following the same rule the upload path
    # uses — free text typed by a human is not something to copy into logs.
    log.info(
        "screening.rejected",
        rejected_by=reviewer.email,
        reviewer_role=reviewer.role,
        reason_chars=len(reason),
        from_gate=at_gate,
    )
    return {
        "thread_id": thread_id,
        "status": "rejected",
        "rejected_by": reviewer.email,
        "rejected_by_role": reviewer.role,
        "rejected_at": rejected_at,
        "rejected_reason": reason,
    }


# Where a run can still be corrected by hand: parked at the gate, escalated after
# the Critic loop gave up, failed — or finished.
#
# "done" was excluded until #95 on the grounds that re-running a scored run
# rewrites its history. The what-if simulator is what changed the reasoning: the
# whole point of showing a coordinator that eGFR ≥ 50 would recover 14 patients is
# that they can then act on it, and the run holding that answer is by definition
# one that has already scored a cohort. It is not a silent rewrite either — the
# edit is stamped with a revision, a diff and an editor (#53), the Critic re-runs
# over it, and no new cohort exists until a named reviewer approves the gate
# again. What the old rule was really protecting is that a run must never show a
# cohort scored against criteria it no longer has, and `_stale_cohort` below
# enforces exactly that instead.
#
# Still absent is "rejected", which is a reviewer's own decision to stop (#91):
# re-opening it by editing would erase the decision rather than reverse it, and
# re-uploading the protocol is the honest way back.
_EDITABLE_STEPS = frozenset({"awaiting_approval", "escalated", "failed", "done"})


def _stale_cohort(values: dict[str, Any]) -> dict[str, Any]:
    """The state update that retires a cohort the edited criteria invalidate.

    A run that has already scored patients keeps `matched_patients` in its
    checkpoint through a re-run, and the re-run parks at the gate before the
    Matcher — so without this the run detail view would show revision 2's criteria
    above revision 1's verdicts, and the runs index would count matches nobody
    approved these criteria for. Empty for a run that never matched, so the common
    edit-at-the-gate path is untouched.

    Discarding is the honest option rather than the lossy one: the cohort was an
    answer to a question the reviewer has just changed, and the previous
    checkpoint still holds it for anyone replaying the thread. The event says how
    many verdicts went, so the timeline records the cost of the decision.
    """
    previous = values.get("matched_patients") or []
    if not previous:
        return {}
    return {
        "matched_patients": [],
        "match_summary": None,
        "events": [
            event(
                "human",
                "edited",
                f"Discarded the previous cohort of {len(previous)} scored patients — it was "
                "matched against the criteria this revision replaces",
            )
        ],
    }


async def resume_with_edited_criteria(
    store: ScreeningStore,
    graph: ScreeningGraph,
    thread_id: str,
    *,
    criteria: dict[str, Any],
    base_revision: int,
    editor: Principal,
) -> AsyncIterator[str]:
    """Write a reviewer's corrected criteria into the checkpoint and re-run (#53).

    The gate used to be approve-only, which left a reviewer looking at a bad
    threshold or a hallucinated criterion with nothing to do but escalate. This is
    the other exit: the edited extraction replaces the parser's, and the run
    continues from there.

    It re-enters the graph **as the parser** (`as_node="parser"`) rather than
    resuming into the matcher. Two consequences, both wanted:

    - The Critic re-runs over the edited criteria, so a human edit cannot smuggle
      a compliance violation (an implausible threshold, a dropped required
      attribute) past the guardrail that exists to catch exactly that. If it
      passes, the run parks at the gate again for an explicit approval; if not, it
      loops or escalates just as a machine extraction would.
    - Patient data is still only touched after someone approves — the audit trail
      (#50) says a *named* reviewer authorized matching, and auto-resuming into
      the matcher here would produce a cohort no one had approved.

    Validation is eager (before any frame is yielded) so an unknown thread, a run
    that can't be edited, and a stale revision are HTTP errors rather than
    failures buried mid-stream. `parse_attempts` is deliberately *not* reset: the
    escalation cap protects the LLM budget, and a reviewer who can hand-edit the
    criteria doesn't need another automated retry to get them right.
    """
    config = await _require_thread(store, thread_id)
    bind_contextvars(thread_id=thread_id)
    snapshot = await graph.aget_state(config)
    values = snapshot.values
    previous = values.get("parsed_criteria")
    at_a_stop = bool(snapshot.next) or values.get("current_step") in _EDITABLE_STEPS
    if previous is None or not at_a_stop:
        raise ScreeningNotEditableError(
            "This screening has no extraction to correct — criteria can be edited while a run is "
            "at the approval gate, escalated, failed, or finished, but not after it was rejected."
        )

    current_revision = int(values.get("criteria_revision") or 0)
    if base_revision != current_revision:
        raise CriteriaRevisionConflictError(
            f"These edits were made against revision {base_revision}, but the criteria are now "
            f"at revision {current_revision} — someone else edited this run. Reload it and "
            "re-apply your changes."
        )

    changes = criteria_edits.diff_criteria(previous, criteria)
    revision = current_revision + 1
    summary = criteria_edits.summarize(changes)
    # Empty unless this run had already scored a cohort — the case #95's promoted
    # what-if introduced. Spread first so the explicit keys below always win, and
    # its event appended after the edit's so the timeline reads cause then cost.
    stale = _stale_cohort(values)
    await graph.aupdate_state(
        config,
        {
            **stale,
            "parsed_criteria": criteria,
            "criteria_revision": revision,
            # Appended, not replaced: `criteria_edits` and `events` both carry the
            # operator.add reducer, so revision N's diff joins revision N-1's.
            "criteria_edits": [criteria_edits.edit_record(revision, editor, changes)],
            # The Critic's previous verdict described the extraction that just got
            # replaced, so it must not survive into the re-run: a stale `passed`
            # would route straight back to the gate, and stale feedback would be
            # fed to the Parser as objections to criteria a human already fixed.
            "compliance_passed": False,
            "critic_feedback": None,
            "current_step": "critiquing",
            "events": [
                event(
                    "human",
                    "edited",
                    f"Criteria revised by {editor.email} ({editor.role}) — {summary} "
                    f"(revision {revision}); re-running compliance review",
                ),
                *stale.get("events", []),
            ],
        },
        as_node="parser",
    )
    log.info(
        "screening.criteria_edited",
        edited_by=editor.email,
        editor_role=editor.role,
        revision=revision,
        changes=len(changes),
    )
    # `as_node="parser"` leaves the graph's next task at the Critic (the parser's
    # own outgoing edge), so a None input resumes there — the same resume the
    # approve path uses, one node earlier.
    return _graph_frames(
        store,
        graph,
        thread_id,
        config,
        None,
        stream_mode=["updates", "custom"],
        operation="rerun",
    )


async def get_screening_state(store: ScreeningStore, graph: ScreeningGraph, thread_id: str) -> dict:
    """The screening's current graph state, its pending (interrupted) nodes, its
    event timeline, and its store row.

    The `screening` block is not redundant with `values`: a screening that was
    uploaded but never streamed has no checkpoint at all, so `values` is `{}` and
    the graph cannot say what phase it is in or even what file it came from. The
    store row always exists, and it is the same row the runs index renders — so
    including it is what keeps a run's detail view from contradicting the list it
    was opened from (#51).

    `timeline` is the audit view of `values["events"]` (#55) — retry rounds,
    Critic rejections, escalation, and who cleared the gate, resolved into the
    order they happened. Derived here rather than in the browser and served on
    this payload rather than behind its own route: everything it reads is already
    in hand, the derivation deserves tests, and the report exported from this
    same payload has to tell the same story the screen does.

    `attrition` is the same arrangement for the cohort (#94): which criteria
    screened patients out, ranked, with the overlap between the top ones. It reads
    only `values["matched_patients"]`, so it costs nothing beyond the checkpoint
    already loaded and is empty for a run that never reached the Matcher.

    `coverage` is the third (#93): how much of the protocol this run could
    actually check, and what it could not. Served on this payload for the same
    reason the other two are — and specifically so the *gate* can show it, since a
    run parked awaiting approval is a run whose checkpoint this endpoint already
    describes. A reviewer sees "we could only check 14 of 20 criteria" while the
    decision is still theirs.

    `usage` is the fourth (#101): what this run's LLM calls consumed and cost,
    split by node. It reads only `values["llm_usage"]`, which the graph appends
    to as it runs, so it costs nothing beyond the checkpoint already loaded and
    is empty for a run that never reached the Parser. It is the per-run half of
    the cost accounting whose instance-wide half is on the metrics summary.
    """
    config = await _require_thread(store, thread_id)
    bind_contextvars(thread_id=thread_id)
    snapshot = await graph.aget_state(config)
    record = await store.get_record(thread_id)
    values = jsonable_encoder(snapshot.values)
    return {
        "values": values,
        "pending": list(snapshot.next),
        "timeline": timeline.build_timeline(values),
        "attrition": attrition.build_attrition(values),
        "coverage": coverage.build_coverage(values),
        "usage": usage.build_usage(values),
        # `_require_thread` just passed, so the row is there; guard anyway rather
        # than assert, since the two reads aren't in one transaction.
        "screening": {
            "thread_id": record.thread_id,
            "source_filename": record.source_filename,
            "status": record.status,
            "created_at": record.created_at,
            "criteria_count": record.criteria_count,
            "match_count": record.match_count,
        }
        if record
        else None,
    }


async def simulate_screening(
    store: ScreeningStore,
    graph: ScreeningGraph,
    thread_id: str,
    overrides: Sequence[simulation.Override],
) -> dict:
    """Re-score this run's cohort under moved thresholds — and change nothing (#95).

    The read-only twin of `resume_with_edited_criteria`. That one writes a
    reviewer's corrected criteria into the checkpoint and re-runs the pipeline;
    this one asks what the cohort *would* look like and hands back the answer,
    leaving the run exactly where it was. The only graph call it makes is
    `aget_state` — there is no `aupdate_state` on this path, which is what makes
    the "without mutating the checkpoint" guarantee structural rather than a
    promise (`services/simulation.py` documents why it also costs no LLM call).

    It is guarded like every other route, at the reviewer rung — the floor of the
    role ladder, and the same rung `/state` sits at. Nothing here is an authority a
    reader of the run does not already have: a simulation reads the verdicts the
    run produced and reaches no further into patient data than the page that
    displays them.

    The response carries the extraction with the overrides applied, so promoting a
    simulation is the existing `PATCH /criteria` call with that payload — the same
    revision check, the same Critic re-run, the same audit entry. No second write
    path exists for a promoted what-if, deliberately: a threshold that reached the
    criteria without passing the Critic would be exactly the hole the gate exists
    to close.
    """
    config = await _require_thread(store, thread_id)
    bind_contextvars(thread_id=thread_id)
    snapshot = await graph.aget_state(config)
    result = simulation.simulate(jsonable_encoder(snapshot.values), overrides)
    log.info(
        "screening.simulated",
        overrides=len(result["overrides"]),
        eligible_delta=result["delta"]["eligible"],
    )
    return dict(result)


async def compare_screenings(
    store: ScreeningStore, graph: ScreeningGraph, a_thread_id: str, b_thread_id: str
) -> dict:
    """Two runs diffed side by side — criteria and cohort (#59).

    Built from two `get_screening_state` payloads rather than from its own reads of
    the checkpoints, so each column of a comparison is the same data that run's own
    detail page renders. `services/comparison.py` owns the reduction and is pure;
    this function is the part that needs the store and the graph.

    Comparing a run with itself is refused (422) rather than answered with an
    all-identical table: it is a mistyped link or a double-clicked checkbox, and a
    page confirming that a run matches itself is a worse answer than saying so.
    Either id being unknown is the 404 `get_screening_state` already raises.

    The two states are fetched in sequence, not concurrently: each is a checkpoint
    read against the same store and graph, and two of them are cheap next to
    holding two `aget_state` calls in flight for what is a read-only view.
    """
    if a_thread_id == b_thread_id:
        raise InvalidComparisonError(
            "A comparison needs two different runs — pick a second screening to compare this one "
            "against."
        )
    a_payload = await get_screening_state(store, graph, a_thread_id)
    b_payload = await get_screening_state(store, graph, b_thread_id)
    # Both ids explicitly: this is the one request that is about two threads, and
    # the bound `thread_id` contextvar can only name one of them (the last one
    # read), so the line has to carry the pair itself to be useful.
    log.info("screening.compared", run_a=a_thread_id, run_b=b_thread_id)
    return comparison.compare_runs(a_payload, b_payload)


async def get_screening_protocol(
    store: ScreeningStore, graph: ScreeningGraph, thread_id: str
) -> dict:
    """The uploaded protocol text plus each criterion's span within it (#54).

    Provenance stops being a claim the moment a reviewer can see the passage a
    criterion was read out of, so this pairs the stored upload with resolved
    character offsets for every distinct `source_text` in the checkpoint.

    The spans are resolved here rather than in the browser for two reasons: the
    protocol and the criteria are both already in hand on this side, and the
    matching is fuzzy enough (whitespace-collapsed, with a longest-prefix
    fallback) to deserve tests. Sentences that cannot be located are absent from
    `spans` — the caller renders that as "not found in the protocol" rather than
    highlighting a guess.

    A run with no checkpoint yet is not an error: the upload is exactly what such
    a run *does* have, so the text comes back with an empty `spans`.
    """
    config = await _require_thread(store, thread_id)
    bind_contextvars(thread_id=thread_id)
    screening_input = await store.get_input(thread_id)
    if screening_input is None:
        raise ScreeningNotFoundError(f"No screening found for thread_id {thread_id}")
    snapshot = await graph.aget_state(config)
    criteria = snapshot.values.get("parsed_criteria")
    text = screening_input.raw_protocol_text
    spans = provenance.locate_all(text, provenance.source_texts(criteria))
    return {
        "thread_id": thread_id,
        "source_filename": screening_input.source_filename,
        "text": text,
        "spans": [
            {"source_text": s.source_text, "start": s.start, "end": s.end, "exact": s.exact}
            for s in spans
        ],
    }


async def get_screening_report(
    store: ScreeningStore, graph: ScreeningGraph, thread_id: str, exporter: Principal
) -> tuple[str, str]:
    """One run rendered as a downloadable report (#56): `(filename, html)`.

    Built from `get_screening_state`'s own payload rather than from a second read
    of the checkpoint, so the exported document and the run detail view are two
    renderings of one snapshot — a report that disagreed with the screen it was
    exported from would be worse than no report.

    A screening that was uploaded but never streamed is a 409
    (`ScreeningNotReportableError`), not an empty document; every other phase has
    something worth handing off. The `.html`/attachment mechanics belong to the
    route — this layer returns the document and the name it should be saved under.

    `exporter` is recorded in the *log*, not in the checkpoint: this document
    carries patient data out of the app, so who took a copy belongs in the same
    correlated stream as who approved the matching (#50) — but a replay must never
    write to the run it is replaying, or opening a finished screening would keep
    amending its state.
    """
    payload = await get_screening_state(store, graph, thread_id)
    if not report.has_reportable_content(payload):
        raise ScreeningNotReportableError(
            "This screening has never run, so there is nothing to report — stream it first."
        )
    log.info("screening.report_exported", exported_by=exporter.email, exporter_role=exporter.role)
    return report.report_filename(payload), report.render_report(payload)
