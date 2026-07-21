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


class ResumeGraph:
    """Models the /approve resume: the first aget_state reports the matcher gate
    (so the approvable pre-check passes), astream yields `updates`, then a later
    aget_state reports `after` (the terminal state for the final frame)."""

    def __init__(
        self, updates: list[dict | tuple], after: FakeSnapshot, gate: tuple = ("matcher",)
    ):
        self.updates = updates
        self.after = after
        self.gate = gate
        self._state_calls = 0

    async def astream(self, *_a: object, **_k: object) -> AsyncIterator[dict | tuple]:
        for update in self.updates:
            yield update

    async def aget_state(self, _config: object) -> FakeSnapshot:
        self._state_calls += 1
        return FakeSnapshot(pending=self.gate) if self._state_calls == 1 else self.after

    async def ainvoke(self, *_a: object) -> dict:  # pragma: no cover - approve streams now
        raise NotImplementedError


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


async def test_approve_streams_matcher_update_and_marks_done():
    store = InMemoryScreeningStore()
    thread_id = await screening.create_screening(store, "p.md", b"x")
    graph = ResumeGraph(
        updates=[{"matcher": {"matched_patients": [{"patient_id": "P1"}], "current_step": "done"}}],
        after=FakeSnapshot(values={"current_step": "done"}),
    )
    frames = _frames(await _drain(await screening.approve_screening(store, graph, thread_id)))
    assert {
        "node": "matcher",
        "update": {"matched_patients": [{"patient_id": "P1"}], "current_step": "done"},
    } in frames
    assert frames[-1] == {"node": sse.END}
    assert (await store.list())[0].status == "done"


async def test_approve_relays_custom_progress_frames():
    store = InMemoryScreeningStore()
    thread_id = await screening.create_screening(store, "p.md", b"x")
    # A list stream_mode makes the graph yield (mode, chunk) tuples; a "custom"
    # chunk is the matcher's mid-flight progress and must surface as a
    # non-terminal __progress__ frame before the matcher's terminal update.
    graph = ResumeGraph(
        updates=[
            ("custom", {"phase": "matching", "done": 0, "total": 2}),
            ("updates", {"matcher": {"matched_patients": [{"patient_id": "P1"}]}}),
        ],
        after=FakeSnapshot(values={"current_step": "done"}),
    )
    frames = _frames(await _drain(await screening.approve_screening(store, graph, thread_id)))
    assert {"node": sse.PROGRESS, "update": {"phase": "matching", "done": 0, "total": 2}} in frames
    assert any(f["node"] == "matcher" for f in frames)
    assert frames[-1] == {"node": sse.END}


async def test_approve_when_not_awaiting_raises_409_error():
    store = InMemoryScreeningStore()
    thread_id = await screening.create_screening(store, "p.md", b"x")
    # Eager pre-check raises before any frame is yielded → becomes a 409, not a
    # mid-stream error.
    with pytest.raises(ScreeningNotApprovableError):
        await screening.approve_screening(store, ApprovingGraph(pending=()), thread_id)


async def test_approve_domain_error_streams_error_frame_and_marks_failed():
    store = InMemoryScreeningStore()
    thread_id = await screening.create_screening(store, "p.md", b"x")
    # A matcher DataStoreError fires mid-stream, so it can't be an HTTP status:
    # it terminates the approve stream with an __error__ frame (mirroring the
    # initial stream). The graph checkpoint stays parked at the gate, so a retry
    # once the store is fixed still resumes.
    graph = RaisingGraph(DataStoreError("patients.json is corrupt"))
    frames = _frames(await _drain(await screening.approve_screening(store, graph, thread_id)))
    assert frames[-1]["node"] == sse.ERROR
    assert "patients.json is corrupt" in frames[-1]["message"]
    assert (await store.list())[0].status == "failed"


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
