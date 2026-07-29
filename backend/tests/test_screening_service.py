"""Service-layer unit tests (#3): the screening use-cases exercised directly,
with an in-memory store and fake graphs — no FastAPI app, no running server.

These prove the business logic lives below the route handlers: input parsing,
state persistence, status denormalization, SSE framing, and the approval gate
are all reachable without an HTTP request.
"""

import json
from collections.abc import AsyncIterator

import pytest

from app.auth import Principal
from app.exceptions import (
    DataStoreError,
    ScreeningNotApprovableError,
    ScreeningNotFoundError,
)
from app.persistence import InMemoryScreeningStore, ScreeningRecord
from app.services import screening, sse
from tests.auth_helpers import REVIEWER


async def _first_row(store: InMemoryScreeningStore) -> ScreeningRecord:
    """The single screening these tests create — the store's list is paged now."""
    return (await store.list(limit=10, offset=0)).items[0]


def _frames(raw: list[str]) -> list[dict]:
    return [json.loads(line.removeprefix("data: ")) for line in raw]


async def _drain(iterator: AsyncIterator[str]) -> list[str]:
    return [frame async for frame in iterator]


async def _approve_frames(
    store: InMemoryScreeningStore,
    graph: screening.ScreeningGraph,
    thread_id: str,
    approver: Principal = REVIEWER,
) -> list[dict]:
    """Run an approval to completion and return its parsed SSE frames."""
    frames = await _drain(await screening.approve_screening(store, graph, thread_id, approver))
    return _frames(frames)


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

    async def aupdate_state(  # pragma: no cover - only the approve path records
        self, _config: object, _values: dict
    ) -> None:
        return None


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

    async def aupdate_state(self, _config: object, _values: dict) -> None:
        """Records the approver (#50). A no-op here: these fakes assert on frames."""
        return None


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

    async def aupdate_state(self, _config: object, _values: dict) -> None:
        """Records the approver (#50). A no-op here: these fakes assert on frames."""
        return None


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
        # What approve_screening wrote into the checkpoint before resuming, and
        # whether it had resumed yet when it did (#50).
        self.recorded: dict | None = None
        self.recorded_before_resume: bool | None = None
        self._streamed = False

    async def astream(self, *_a: object, **_k: object) -> AsyncIterator[dict | tuple]:
        self._streamed = True
        for update in self.updates:
            yield update

    async def aget_state(self, _config: object) -> FakeSnapshot:
        self._state_calls += 1
        return FakeSnapshot(pending=self.gate) if self._state_calls == 1 else self.after

    async def ainvoke(self, *_a: object) -> dict:  # pragma: no cover - approve streams now
        raise NotImplementedError

    async def aupdate_state(self, _config: object, values: dict) -> None:
        self.recorded = values
        self.recorded_before_resume = not self._streamed


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
    page = await screening.list_screenings(store, limit=10, offset=0)
    assert {r["source_filename"] for r in page["items"]} == {"a.md", "b.md"}
    assert all(
        {"thread_id", "status", "created_at", "criteria_count", "match_count"} <= r.keys()
        for r in page["items"]
    )
    assert (page["total"], page["limit"], page["offset"]) == (2, 10, 0)


async def test_list_pages_without_losing_the_total():
    """`total` counts every match, not the page — it is how the UI knows there's
    a next page at all."""
    store = InMemoryScreeningStore()
    for name in ("a.md", "b.md", "c.md"):
        await screening.create_screening(store, name, b"x")
    first = await screening.list_screenings(store, limit=2, offset=0)
    second = await screening.list_screenings(store, limit=2, offset=2)
    assert len(first["items"]) == 2
    assert len(second["items"]) == 1
    assert first["total"] == second["total"] == 3
    # No row appears on both pages.
    assert not {r["thread_id"] for r in first["items"]} & {r["thread_id"] for r in second["items"]}


async def test_list_filters_by_status_and_search():
    store = InMemoryScreeningStore()
    parked = await screening.create_screening(store, "nsclc-protocol.md", b"x")
    await screening.create_screening(store, "ckd-protocol.md", b"y")
    await store.set_status(parked, "awaiting_approval")

    by_status = await screening.list_screenings(
        store, limit=10, offset=0, status="awaiting_approval"
    )
    assert [r["thread_id"] for r in by_status["items"]] == [parked]
    assert by_status["total"] == 1

    # Case-insensitive substring of the filename.
    by_name = await screening.list_screenings(store, limit=10, offset=0, search="NSCLC")
    assert [r["thread_id"] for r in by_name["items"]] == [parked]

    # Filters combine with AND, so a mismatched pair returns nothing.
    neither = await screening.list_screenings(
        store, limit=10, offset=0, status="done", search="ckd"
    )
    assert neither["items"] == [] and neither["total"] == 0


async def test_terminal_frame_denormalizes_criteria_and_match_counts():
    """The runs index reads counts off the row, so a finished run has to write
    them — and `match_count` is the eligible cohort, not everyone scored."""
    store = InMemoryScreeningStore()
    thread_id = await screening.create_screening(store, "p.md", b"x")
    values = {
        "current_step": "done",
        "parsed_criteria": {
            "inclusion_quantitative": [{"attribute": "age"}],
            "inclusion_categorical": [{"value": "NSCLC"}],
            "exclusion_quantitative": [],
            "exclusion_categorical": [{"value": "pregnancy"}],
            # Not a criterion the parser produced — must not inflate the count.
            "unparseable": ["some sentence", "another"],
        },
        "matched_patients": [
            {"patient_id": "P1", "eligible": True, "needs_review": False},
            {"patient_id": "P2", "eligible": False, "needs_review": False},
            # Eligible but indeterminate: a human still has to decide, so it is
            # not a match (mirrors the cohort table's bucketing).
            {"patient_id": "P3", "eligible": True, "needs_review": True},
        ],
    }
    graph = StreamingGraph(updates=[], snapshot=FakeSnapshot(values=values))
    await _drain(await screening.stream_screening(store, graph, thread_id))

    row = await _first_row(store)
    assert (row.criteria_count, row.match_count) == (3, 1)


async def test_failure_after_a_successful_phase_keeps_the_counts():
    """A failed re-run records the failure without erasing what an earlier phase
    already established — the index would otherwise show 0 criteria for a run
    whose criteria are right there on its detail page."""
    store = InMemoryScreeningStore()
    thread_id = await screening.create_screening(store, "p.md", b"x")
    parsed = {"inclusion_quantitative": [{"attribute": "age"}]}
    ok = StreamingGraph(
        updates=[],
        snapshot=FakeSnapshot(
            values={"current_step": "awaiting_approval", "parsed_criteria": parsed}
        ),
    )
    await _drain(await screening.stream_screening(store, ok, thread_id))
    assert (await _first_row(store)).criteria_count == 1

    await _drain(
        await screening.stream_screening(store, RaisingGraph(RuntimeError("boom")), thread_id)
    )
    row = await _first_row(store)
    assert row.status == "failed"
    assert row.criteria_count == 1


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
    assert (await _first_row(store)).status == "done"


async def test_stream_interrupt_ends_with_interrupt_and_awaiting_status():
    store = InMemoryScreeningStore()
    thread_id = await screening.create_screening(store, "p.md", b"x")
    graph = StreamingGraph(
        updates=[{"critic": {"current_step": "awaiting_approval"}}],
        snapshot=FakeSnapshot(values={"current_step": "awaiting_approval"}, pending=("matcher",)),
    )
    frames = _frames(await _drain(await screening.stream_screening(store, graph, thread_id)))
    assert frames[-1] == {"node": sse.INTERRUPT}
    assert (await _first_row(store)).status == "awaiting_approval"


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
    assert (await _first_row(store)).status == "failed"


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
    frames = await _approve_frames(store, graph, thread_id)
    assert {
        "node": "matcher",
        "update": {"matched_patients": [{"patient_id": "P1"}], "current_step": "done"},
    } in frames
    assert frames[-1] == {"node": sse.END}
    assert (await _first_row(store)).status == "done"


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
    frames = await _approve_frames(store, graph, thread_id)
    assert {"node": sse.PROGRESS, "update": {"phase": "matching", "done": 0, "total": 2}} in frames
    assert any(f["node"] == "matcher" for f in frames)
    assert frames[-1] == {"node": sse.END}


async def test_approve_records_the_approver_before_resuming():
    """Audit trail (#50): the approver is stamped into the checkpoint, and it
    happens *before* the resume — so the authorization is durable even if the
    matcher then dies, and the matcher itself can read it out of state."""
    store = InMemoryScreeningStore()
    thread_id = await screening.create_screening(store, "p.md", b"x")
    graph = ResumeGraph(
        updates=[{"matcher": {"matched_patients": []}}],
        after=FakeSnapshot(values={"current_step": "done"}),
    )
    await _approve_frames(store, graph, thread_id)

    assert graph.recorded is not None
    assert graph.recorded["approved_by"] == REVIEWER.email
    assert graph.recorded["approved_by_role"] == REVIEWER.role
    assert graph.recorded["approved_at"]
    assert graph.recorded_before_resume is True
    # `events` has an append reducer, so this joins the run's log rather than
    # replacing it — it must be a list of one, not a bare event.
    (audit_event,) = graph.recorded["events"]
    assert audit_event["agent"] == "human"
    assert audit_event["status"] == "approved"
    assert REVIEWER.email in audit_event["detail"]


async def test_approve_does_not_record_an_approver_when_not_at_the_gate():
    """The 409 pre-check runs first, so a rejected approve leaves no audit entry
    claiming someone authorized a run that never resumed."""
    store = InMemoryScreeningStore()
    thread_id = await screening.create_screening(store, "p.md", b"x")
    graph = ResumeGraph(updates=[], after=FakeSnapshot(values={"current_step": "done"}), gate=())
    with pytest.raises(ScreeningNotApprovableError):
        await screening.approve_screening(store, graph, thread_id, REVIEWER)
    assert graph.recorded is None


async def test_approve_when_not_awaiting_raises_409_error():
    store = InMemoryScreeningStore()
    thread_id = await screening.create_screening(store, "p.md", b"x")
    # Eager pre-check raises before any frame is yielded → becomes a 409, not a
    # mid-stream error.
    with pytest.raises(ScreeningNotApprovableError):
        await screening.approve_screening(store, ApprovingGraph(pending=()), thread_id, REVIEWER)


async def test_approve_domain_error_streams_error_frame_and_marks_failed():
    store = InMemoryScreeningStore()
    thread_id = await screening.create_screening(store, "p.md", b"x")
    # A matcher DataStoreError fires mid-stream, so it can't be an HTTP status:
    # it terminates the approve stream with an __error__ frame (mirroring the
    # initial stream). The graph checkpoint stays parked at the gate, so a retry
    # once the store is fixed still resumes.
    graph = RaisingGraph(DataStoreError("patients.json is corrupt"))
    frames = await _approve_frames(store, graph, thread_id)
    assert frames[-1]["node"] == sse.ERROR
    assert "patients.json is corrupt" in frames[-1]["message"]
    assert (await _first_row(store)).status == "failed"


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


async def test_get_state_carries_the_store_row():
    """The detail view needs the authoritative metadata, not just the checkpoint."""
    store = InMemoryScreeningStore()
    thread_id = await screening.create_screening(store, "p.md", b"x")
    await store.set_status(thread_id, "done", criteria_count=4, match_count=2)
    graph = StreamingGraph(updates=[], snapshot=FakeSnapshot(values={"current_step": "done"}))
    row = (await screening.get_screening_state(store, graph, thread_id))["screening"]
    assert row["thread_id"] == thread_id
    assert row["source_filename"] == "p.md"
    assert (row["status"], row["criteria_count"], row["match_count"]) == ("done", 4, 2)


async def test_get_state_of_a_never_streamed_run_still_reports_its_status():
    """Regression: a screening uploaded but never streamed has NO checkpoint, so
    `values` is empty and the graph can say nothing about it. Without the store
    row the detail view had to invent a phase, and rendered such a run as a green
    "done" while the runs index correctly showed "routing"."""
    store = InMemoryScreeningStore()
    thread_id = await screening.create_screening(store, "p.md", b"x")
    graph = StreamingGraph(updates=[], snapshot=FakeSnapshot(values={}))
    state = await screening.get_screening_state(store, graph, thread_id)
    assert state["values"] == {}
    assert state["pending"] == []
    assert state["screening"]["status"] == "routing"
    assert state["screening"]["source_filename"] == "p.md"
