"""Screening use-cases — create, stream, approve, inspect — orchestrating the
store and the graph so route handlers stay thin HTTP translators.

Routes hand this layer raw inputs (upload bytes, a thread_id) and the wired
dependencies (`store`, `graph`); it owns everything in between — input parsing,
state construction, graph invocation, status denormalization, and SSE framing.
The graph is assembled here (`build_screening_graph`) so `app/main.py` never
imports the graph builder directly.
"""

from __future__ import annotations

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
    ) -> AsyncIterator[dict[str, Any]]: ...

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


async def create_screening(store: ScreeningStore, filename: str | None, raw: bytes) -> str:
    """Parse the upload into eligibility text, persist it, and return its thread_id."""
    if filename and filename.lower().endswith(".pdf"):
        text = extract_eligibility_text(raw)
    else:
        text = raw.decode("utf-8", errors="replace")

    thread_id = str(uuid4())
    bind_contextvars(thread_id=thread_id)
    # Persist the input durably so a restart between upload and stream — or a
    # second worker handling the stream — loses nothing. The graph's execution
    # state is rebuilt from initial_state() when the run first streams.
    await store.create(thread_id, filename or "upload", text)
    # PHI hygiene: log the size of the upload, never its contents.
    log.info("screening.created", source_filename=filename or "upload", text_chars=len(text))
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


async def approve_screening(store: ScreeningStore, graph: ScreeningGraph, thread_id: str) -> dict:
    """Resume a screening past the human-in-the-loop gate and return its result."""
    config = await _require_thread(store, thread_id)
    bind_contextvars(thread_id=thread_id)
    if not (await graph.aget_state(config)).next:
        raise ScreeningNotApprovableError("screening is not awaiting approval")
    log.info("screening.approved")
    # Resume past the interrupt_before=["matcher"] gate. A DataStoreError from
    # the matcher propagates to the error handler (503) and the screening stays
    # parked at the gate, so approval can be retried once fixed.
    result = await graph.ainvoke(None, config)
    await store.set_status(thread_id, str(result.get("current_step") or "done"))
    return {
        "matched_patients": result["matched_patients"],
        "events": jsonable_encoder(result["events"]),
    }


async def get_screening_state(store: ScreeningStore, graph: ScreeningGraph, thread_id: str) -> dict:
    """The screening's current graph state plus any pending (interrupted) nodes."""
    config = await _require_thread(store, thread_id)
    bind_contextvars(thread_id=thread_id)
    snapshot = await graph.aget_state(config)
    return {"values": jsonable_encoder(snapshot.values), "pending": list(snapshot.next)}
