"""Screening use-cases — create, stream, approve, inspect — orchestrating the
store and the graph so route handlers stay thin HTTP translators.

Routes hand this layer raw inputs (upload bytes, a thread_id) and the wired
dependencies (`store`, `graph`); it owns everything in between — input parsing,
state construction, graph invocation, status denormalization, and SSE framing.
The graph is assembled here (`build_screening_graph`) so `app/main.py` never
imports the graph builder directly.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, Protocol
from uuid import uuid4

from fastapi.encoders import jsonable_encoder

from app.exceptions import ScreenerError, ScreeningNotApprovableError, ScreeningNotFoundError
from app.graph.builder import build_graph
from app.graph.state import initial_state
from app.logging_config import bind_contextvars, get_logger
from app.services import sse
from app.services.pdf import extract_eligibility_text
from app.services.uploads import sanitize_filename

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.graph.state import CompiledStateGraph

    from app.persistence import ScreeningStore

log = get_logger("screening")


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


async def list_screenings(store: ScreeningStore) -> list[dict]:
    """All screenings, newest first — backs the dashboard list view."""
    records = await store.list()
    return [
        {
            "thread_id": r.thread_id,
            "source_filename": r.source_filename,
            "status": r.status,
            "created_at": r.created_at,
        }
        for r in records
    ]


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

    async def generate() -> AsyncIterator[str]:
        # An exception mid-stream can't become an HTTP error (headers are
        # already sent) — the terminal __error__ frame is the error channel,
        # and it must catch everything or the frontend hangs forever.
        try:
            state = initial_state(
                screening_input.raw_protocol_text, screening_input.source_filename
            )
            async for chunk in graph.astream(state, config, stream_mode="updates"):
                for node, update in chunk.items():
                    yield sse.update_frame(node, jsonable_encoder(update))
            snapshot = await graph.aget_state(config)
            await store.set_status(thread_id, _status_from_snapshot(snapshot))
            yield _terminal_frame(snapshot)
            log.info("screening.stream_finished")
        except ScreenerError as exc:
            log.warning("screening.stream_error", error=type(exc).__name__, detail=str(exc))
            await store.set_status(thread_id, "failed")
            yield sse.error_frame(str(exc))
        except Exception:  # noqa: BLE001 — last-resort stream terminator, detail stays server-side
            log.error("screening.stream_crashed", exc_info=True)
            await store.set_status(thread_id, "failed")
            yield sse.error_frame("Screening failed unexpectedly — check server logs.")

    return generate()


async def approve_screening(
    store: ScreeningStore, graph: ScreeningGraph, thread_id: str
) -> AsyncIterator[str]:
    """Resume past the human-in-the-loop gate and STREAM the matcher over SSE.

    The matcher makes LLM calls over the whole cohort and can run for minutes on
    a local model. Streaming it — instead of blocking the POST until it returns —
    keeps approval responsive (a slow model no longer times out the client) and
    reuses the exact frame/error contract `stream_screening` uses: the matcher's
    node update carries `matched_patients`, then a terminal frame closes the run.

    Validation is eager (raising before any frame is yielded) so an unknown
    thread or a screening not at the gate becomes an HTTP error, not a frame
    buried after the response headers are already sent. That second check also
    rejects an approve on an already-finished screening (its `next` is empty).
    """
    config = await _require_thread(store, thread_id)
    bind_contextvars(thread_id=thread_id)
    if not (await graph.aget_state(config)).next:
        raise ScreeningNotApprovableError("screening is not awaiting approval")
    log.info("screening.approved")

    async def generate() -> AsyncIterator[str]:
        # Mirrors stream_screening's generator: an exception mid-stream can't be
        # an HTTP status (headers already sent), so the terminal __error__ frame
        # is the only error channel and must catch everything.
        try:
            # None input resumes from the interrupt_before=["matcher"] checkpoint.
            # Two stream modes: "updates" carries the matcher's terminal node
            # result; "custom" carries its mid-flight progress (see matcher's
            # _progress_emitter) so the stream emits real frames during the long
            # LLM matching pass, keeping the idle-timeout reaper from killing a
            # working run. With a list mode LangGraph yields (mode, chunk) tuples;
            # the fakes yield bare dicts, so treat a non-tuple as an update chunk.
            async for item in graph.astream(None, config, stream_mode=["updates", "custom"]):
                mode, chunk = item if isinstance(item, tuple) else ("updates", item)
                if mode == "custom":
                    yield sse.progress_frame(jsonable_encoder(chunk))
                    continue
                for node, update in chunk.items():
                    yield sse.update_frame(node, jsonable_encoder(update))
            snapshot = await graph.aget_state(config)
            await store.set_status(thread_id, _status_from_snapshot(snapshot))
            yield _terminal_frame(snapshot)
            log.info("screening.approve_finished")
        except ScreenerError as exc:
            log.warning("screening.approve_error", error=type(exc).__name__, detail=str(exc))
            await store.set_status(thread_id, "failed")
            yield sse.error_frame(str(exc))
        except Exception:  # noqa: BLE001 — last-resort terminator, detail stays server-side
            log.error("screening.approve_crashed", exc_info=True)
            await store.set_status(thread_id, "failed")
            yield sse.error_frame("Screening failed unexpectedly — check server logs.")

    return generate()


async def get_screening_state(store: ScreeningStore, graph: ScreeningGraph, thread_id: str) -> dict:
    """The screening's current graph state plus any pending (interrupted) nodes."""
    config = await _require_thread(store, thread_id)
    bind_contextvars(thread_id=thread_id)
    snapshot = await graph.aget_state(config)
    return {"values": jsonable_encoder(snapshot.values), "pending": list(snapshot.next)}
