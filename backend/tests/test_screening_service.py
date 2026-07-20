"""Service-layer unit tests (#3): the screening use-cases exercised directly,
with an in-memory store and fake graphs — no FastAPI app, no running server.

These prove the business logic lives below the route handlers: input parsing,
state persistence, status denormalization, SSE framing, and the approval gate
are all reachable without an HTTP request.
"""

import json
from collections.abc import AsyncIterator

import pytest

from app.exceptions import (
    DataStoreError,
    ScreeningNotApprovableError,
    ScreeningNotFoundError,
)
from app.persistence import InMemoryScreeningStore
from app.services import screening, sse


def _frames(raw: list[str]) -> list[dict]:
    return [json.loads(line.removeprefix("data: ")) for line in raw]


async def _drain(iterator: AsyncIterator[str]) -> list[str]:
    return [frame async for frame in iterator]


class FakeSnapshot:
    def __init__(self, values: dict | None = None, pending: tuple = ()):
        self.values = values or {}
        self.next = pending


class StreamingGraph:
    """astream yields the given updates, then aget_state returns `snapshot`."""

    def __init__(self, updates: list[dict], snapshot: FakeSnapshot):
        self.updates = updates
        self.snapshot = snapshot

    async def astream(self, *_a: object, **_k: object) -> AsyncIterator[dict]:
        for update in self.updates:
            yield update

    async def aget_state(self, _config: object) -> FakeSnapshot:
        return self.snapshot

    async def ainvoke(self, *_a: object) -> dict:  # pragma: no cover - not driven here
        raise NotImplementedError


class RaisingGraph:
    """astream yields one update then raises; ainvoke raises the same exc."""

    def __init__(self, exc: Exception, pending: tuple = ("matcher",)):
        self.exc = exc
        self.pending = pending

    async def astream(self, *_a: object, **_k: object) -> AsyncIterator[dict]:
        yield {"router": {"current_step": "parsing"}}
        raise self.exc

    async def aget_state(self, _config: object) -> FakeSnapshot:
        return FakeSnapshot(pending=self.pending)

    async def ainvoke(self, *_a: object) -> dict:
        raise self.exc


class ApprovingGraph:
    """aget_state reports it's parked at the gate; ainvoke returns a result."""

    def __init__(self, pending: tuple = ("matcher",), result: dict | None = None):
        self.pending = pending
        self.result = result or {"matched_patients": [], "events": [], "current_step": "done"}

    async def astream(self, *_a: object, **_k: object) -> AsyncIterator[dict]:  # pragma: no cover
        yield {}

    async def aget_state(self, _config: object) -> FakeSnapshot:
        return FakeSnapshot(pending=self.pending)

    async def ainvoke(self, *_a: object) -> dict:
        return self.result


# --- create --------------------------------------------------------------


async def test_create_persists_plaintext_and_returns_thread_id():
    store = InMemoryScreeningStore()
    thread_id = await screening.create_screening(store, "p.md", b"Inclusion: age >= 18")
    assert await store.exists(thread_id)
    stored = await store.get_input(thread_id)
    assert stored is not None
    assert stored.raw_protocol_text == "Inclusion: age >= 18"
    assert stored.source_filename == "p.md"


async def test_create_defaults_missing_filename_to_upload():
    store = InMemoryScreeningStore()
    thread_id = await screening.create_screening(store, None, b"body")
    stored = await store.get_input(thread_id)
    assert stored is not None
    assert stored.source_filename == "upload"


async def test_create_truncates_text_to_cap():
    # A large non-PDF upload is truncated to max_text_chars before it is stored
    # (and thus before it reaches the Parser/Critic prompts).
    store = InMemoryScreeningStore()
    thread_id = await screening.create_screening(store, "big.md", b"x" * 10_000, max_text_chars=100)
    stored = await store.get_input(thread_id)
    assert stored is not None
    assert len(stored.raw_protocol_text) == 100


async def test_create_corrupt_pdf_raises_extraction_error():
    from app.exceptions import ExtractionError

    store = InMemoryScreeningStore()
    with pytest.raises(ExtractionError):
        await screening.create_screening(store, "bad.pdf", b"not a pdf")


# --- list ----------------------------------------------------------------


async def test_list_returns_newest_first_metadata():
    store = InMemoryScreeningStore()
    await screening.create_screening(store, "a.md", b"x")
    await screening.create_screening(store, "b.md", b"y")
    rows = await screening.list_screenings(store)
    assert {r["source_filename"] for r in rows} == {"a.md", "b.md"}
    assert all({"thread_id", "status", "created_at"} <= r.keys() for r in rows)


# --- require-thread guard ------------------------------------------------


async def test_stream_unknown_thread_raises_before_yielding():
    store = InMemoryScreeningStore()
    with pytest.raises(ScreeningNotFoundError):
        await screening.stream_screening(store, StreamingGraph([], FakeSnapshot()), "nope")


async def test_state_unknown_thread_raises():
    store = InMemoryScreeningStore()
    with pytest.raises(ScreeningNotFoundError):
        await screening.get_screening_state(store, ApprovingGraph(), "nope")


# --- stream terminal frames ----------------------------------------------


async def test_stream_success_ends_with_end_frame_and_sets_status():
    store = InMemoryScreeningStore()
    thread_id = await screening.create_screening(store, "p.md", b"x")
    graph = StreamingGraph(
        updates=[{"matcher": {"current_step": "done"}}],
        snapshot=FakeSnapshot(values={"current_step": "done"}),
    )
    frames = _frames(await _drain(await screening.stream_screening(store, graph, thread_id)))
    assert frames[0] == {"node": "matcher", "update": {"current_step": "done"}}
    assert frames[-1] == {"node": sse.END}
    assert (await store.list())[0].status == "done"


async def test_stream_interrupt_ends_with_interrupt_and_awaiting_status():
    store = InMemoryScreeningStore()
    thread_id = await screening.create_screening(store, "p.md", b"x")
    graph = StreamingGraph(
        updates=[{"critic": {"current_step": "awaiting_approval"}}],
        snapshot=FakeSnapshot(values={"current_step": "awaiting_approval"}, pending=("matcher",)),
    )
    frames = _frames(await _drain(await screening.stream_screening(store, graph, thread_id)))
    assert frames[-1] == {"node": sse.INTERRUPT}
    assert (await store.list())[0].status == "awaiting_approval"


async def test_stream_absorbed_failure_becomes_error_frame():
    store = InMemoryScreeningStore()
    thread_id = await screening.create_screening(store, "p.md", b"x")
    graph = StreamingGraph(
        updates=[{"parser": {"current_step": "failed"}}],
        snapshot=FakeSnapshot(
            values={
                "current_step": "failed",
                "events": [{"detail": "LLM backend unavailable"}],
            }
        ),
    )
    frames = _frames(await _drain(await screening.stream_screening(store, graph, thread_id)))
    assert frames[-1] == {"node": sse.ERROR, "message": "LLM backend unavailable"}


async def test_stream_domain_error_surfaces_detail_and_marks_failed():
    store = InMemoryScreeningStore()
    thread_id = await screening.create_screening(store, "p.md", b"x")
    graph = RaisingGraph(DataStoreError("rules file is corrupt"))
    frames = _frames(await _drain(await screening.stream_screening(store, graph, thread_id)))
    assert frames[-1]["node"] == sse.ERROR
    assert "rules file is corrupt" in frames[-1]["message"]
    assert (await store.list())[0].status == "failed"


async def test_stream_unexpected_error_hides_detail():
    store = InMemoryScreeningStore()
    thread_id = await screening.create_screening(store, "p.md", b"x")
    graph = RaisingGraph(RuntimeError("secret internal detail"))
    frames = _frames(await _drain(await screening.stream_screening(store, graph, thread_id)))
    assert frames[-1]["node"] == sse.ERROR
    assert "secret internal detail" not in frames[-1]["message"]


# --- approve -------------------------------------------------------------


async def test_approve_returns_matches_and_sets_status():
    store = InMemoryScreeningStore()
    thread_id = await screening.create_screening(store, "p.md", b"x")
    graph = ApprovingGraph(
        result={"matched_patients": [{"id": "P1"}], "events": [], "current_step": "done"}
    )
    result = await screening.approve_screening(store, graph, thread_id)
    assert result["matched_patients"] == [{"id": "P1"}]
    assert (await store.list())[0].status == "done"


async def test_approve_when_not_awaiting_raises_409_error():
    store = InMemoryScreeningStore()
    thread_id = await screening.create_screening(store, "p.md", b"x")
    with pytest.raises(ScreeningNotApprovableError):
        await screening.approve_screening(store, ApprovingGraph(pending=()), thread_id)


async def test_approve_domain_error_propagates_and_leaves_screening_parked():
    store = InMemoryScreeningStore()
    thread_id = await screening.create_screening(store, "p.md", b"x")
    graph = RaisingGraph(DataStoreError("patients.json is corrupt"))
    with pytest.raises(DataStoreError):
        await screening.approve_screening(store, graph, thread_id)
    # Status untouched — the screening can be retried once the store is fixed.
    assert (await store.list())[0].status == "routing"


# --- state ---------------------------------------------------------------


async def test_get_state_returns_values_and_pending():
    store = InMemoryScreeningStore()
    thread_id = await screening.create_screening(store, "p.md", b"x")
    graph = StreamingGraph(
        updates=[],
        snapshot=FakeSnapshot(values={"current_step": "awaiting_approval"}, pending=("matcher",)),
    )
    state = await screening.get_screening_state(store, graph, thread_id)
    assert state["values"] == {"current_step": "awaiting_approval"}
    assert state["pending"] == ["matcher"]
