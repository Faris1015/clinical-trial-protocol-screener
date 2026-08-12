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
from app.config import Settings
from app.exceptions import (
    CriteriaRevisionConflictError,
    DataStoreError,
    ScreeningNotApprovableError,
    ScreeningNotEditableError,
    ScreeningNotFoundError,
    ScreeningNotRejectableError,
)
from app.persistence import (
    InMemoryAuditStore,
    InMemoryRuleStore,
    InMemoryScreeningStore,
    ScreeningRecord,
)
from app.services import metrics, notifications, screening, sse
from tests.auth_helpers import REVIEWER


async def _first_row(store: InMemoryScreeningStore) -> ScreeningRecord:
    """The single screening these tests create — the store's list is paged now."""
    return (await store.list(limit=10, offset=0)).items[0]


def _audit() -> InMemoryAuditStore:
    """A throwaway decision index (#98) for a test that is not about one.

    Every path below now writes its decision to the org-wide index as well as to
    the checkpoint. *What* it writes is asserted in `test_audit.py`; here the index
    only has to exist, so each call gets a fresh one.
    """
    return InMemoryAuditStore()


def _rules() -> InMemoryRuleStore:
    """An empty rules table (#97) for a test that is not about the Critic's rules.

    Empty rather than seeded, deliberately: these tests drive fake graphs, so the
    deterministic layer never runs, and a seeded table would only add a fixture
    whose contents no assertion here reads. What the engine does with a populated
    table is `test_rules_api.py` and `test_critic_rules.py`.
    """
    return InMemoryRuleStore()


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
    frames = await _drain(
        await screening.approve_screening(store, _audit(), graph, thread_id, approver)
    )
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

    async def aupdate_state(  # pragma: no cover - only the approve/edit paths record
        self, _config: object, _values: dict, as_node: str | None = None
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

    async def aupdate_state(
        self, _config: object, _values: dict, as_node: str | None = None
    ) -> None:
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

    async def aupdate_state(
        self, _config: object, _values: dict, as_node: str | None = None
    ) -> None:
        """Records the approver (#50). A no-op here: these fakes assert on frames."""
        return None


class ResumeGraph:
    """Models a resume: the first aget_state reports where the run is parked (so
    the approvable / editable pre-check passes), astream yields `updates`, then a
    later aget_state reports `after` (the terminal state for the final frame).

    Drives both /approve and the edit-and-rerun path (#53) — `values` seeds the
    parked snapshot the latter reads its previous criteria and revision from."""

    def __init__(
        self,
        updates: list[dict | tuple],
        after: FakeSnapshot,
        gate: tuple = ("matcher",),
        values: dict | None = None,
    ):
        self.updates = updates
        self.after = after
        self.gate = gate
        self.values = values or {}
        self._state_calls = 0
        # What approve_screening wrote into the checkpoint before resuming, and
        # whether it had resumed yet when it did (#50).
        self.recorded: dict | None = None
        self.recorded_before_resume: bool | None = None
        # Which node the write posed as (#53): the edit path rewinds the cursor to
        # the parser so the Critic re-runs; approve leaves it where it is.
        self.recorded_as_node: str | None = None
        self._streamed = False

    async def astream(self, *_a: object, **_k: object) -> AsyncIterator[dict | tuple]:
        self._streamed = True
        for update in self.updates:
            yield update

    async def aget_state(self, _config: object) -> FakeSnapshot:
        self._state_calls += 1
        if self._state_calls == 1:
            return FakeSnapshot(values=self.values, pending=self.gate)
        return self.after

    async def ainvoke(self, *_a: object) -> dict:  # pragma: no cover - approve streams now
        raise NotImplementedError

    async def aupdate_state(
        self, _config: object, values: dict, as_node: str | None = None
    ) -> None:
        self.recorded = values
        self.recorded_as_node = as_node
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
    await _drain(await screening.stream_screening(store, _audit(), _rules(), graph, thread_id))

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
    await _drain(await screening.stream_screening(store, _audit(), _rules(), ok, thread_id))
    assert (await _first_row(store)).criteria_count == 1

    await _drain(
        await screening.stream_screening(
            store, _audit(), _rules(), RaisingGraph(RuntimeError("boom")), thread_id
        )
    )
    row = await _first_row(store)
    assert row.status == "failed"
    assert row.criteria_count == 1


# --- require-thread guard ------------------------------------------------


async def test_stream_unknown_thread_raises_before_yielding():
    store = InMemoryScreeningStore()
    with pytest.raises(ScreeningNotFoundError):
        await screening.stream_screening(
            store, _audit(), _rules(), StreamingGraph([], FakeSnapshot()), "nope"
        )


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
    frames = _frames(
        await _drain(await screening.stream_screening(store, _audit(), _rules(), graph, thread_id))
    )
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
    frames = _frames(
        await _drain(await screening.stream_screening(store, _audit(), _rules(), graph, thread_id))
    )
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
    frames = _frames(
        await _drain(await screening.stream_screening(store, _audit(), _rules(), graph, thread_id))
    )
    assert frames[-1] == {"node": sse.ERROR, "message": "LLM backend unavailable"}


async def test_stream_domain_error_surfaces_detail_and_marks_failed():
    store = InMemoryScreeningStore()
    thread_id = await screening.create_screening(store, "p.md", b"x")
    graph = RaisingGraph(DataStoreError("rules file is corrupt"))
    frames = _frames(
        await _drain(await screening.stream_screening(store, _audit(), _rules(), graph, thread_id))
    )
    assert frames[-1]["node"] == sse.ERROR
    assert "rules file is corrupt" in frames[-1]["message"]
    assert (await _first_row(store)).status == "failed"


async def test_stream_unexpected_error_hides_detail():
    store = InMemoryScreeningStore()
    thread_id = await screening.create_screening(store, "p.md", b"x")
    graph = RaisingGraph(RuntimeError("secret internal detail"))
    frames = _frames(
        await _drain(await screening.stream_screening(store, _audit(), _rules(), graph, thread_id))
    )
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
        await screening.approve_screening(store, _audit(), graph, thread_id, REVIEWER)
    assert graph.recorded is None


async def test_approve_when_not_awaiting_raises_409_error():
    store = InMemoryScreeningStore()
    thread_id = await screening.create_screening(store, "p.md", b"x")
    # Eager pre-check raises before any frame is yielded → becomes a 409, not a
    # mid-stream error.
    with pytest.raises(ScreeningNotApprovableError):
        await screening.approve_screening(
            store, _audit(), ApprovingGraph(pending=()), thread_id, REVIEWER
        )


async def test_approve_of_a_run_parked_before_the_critic_is_rejected():
    """A thread parked at the Critic is pending, but it is not the patient-data
    gate — an edit-and-rerun (#53) whose client vanished between the checkpoint
    write and the resume leaves exactly that state. Approving it would resume the
    Critic while stamping `approved_by`, which must only ever mean "authorized
    patient matching"."""
    store = InMemoryScreeningStore()
    thread_id = await screening.create_screening(store, "p.md", b"x")
    graph = ResumeGraph(updates=[], after=FakeSnapshot(), gate=("critic",))
    with pytest.raises(ScreeningNotApprovableError):
        await screening.approve_screening(store, _audit(), graph, thread_id, REVIEWER)
    assert graph.recorded is None


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


# --- reject (#91) ---------------------------------------------------------


async def _reject(
    store: InMemoryScreeningStore,
    graph: screening.ScreeningGraph,
    thread_id: str,
    reason: str = "Not an oncology protocol — wrong document.",
) -> dict:
    return await screening.reject_screening(store, _audit(), graph, thread_id, REVIEWER, reason)


async def test_reject_at_the_gate_records_the_decision_and_terminates_the_run():
    """The audit trail the issue asks for (#91), written the way approval is: who,
    when, and — unlike approval — why. `as_node="matcher"` is what moves the
    cursor past the interrupt, so the thread stops being pending without the
    matcher ever running."""
    store = InMemoryScreeningStore()
    thread_id = await screening.create_screening(store, "p.md", b"x")
    graph = ResumeGraph(updates=[], after=FakeSnapshot(), values={"parsed_criteria": _criteria()})

    result = await _reject(store, graph, thread_id)

    assert graph.recorded is not None
    assert graph.recorded["rejected_by"] == REVIEWER.email
    assert graph.recorded["rejected_by_role"] == REVIEWER.role
    assert graph.recorded["rejected_at"]
    assert graph.recorded["rejected_reason"] == "Not an oncology protocol — wrong document."
    assert graph.recorded["current_step"] == "rejected"
    assert graph.recorded_as_node == "matcher"
    assert result["status"] == "rejected"
    assert result["rejected_by"] == REVIEWER.email


async def test_reject_joins_the_event_log_rather_than_replacing_it():
    """`events` carries an append reducer, so the rejection has to arrive as a
    list of one — and it is logged under `human`, which is what tells it apart
    from the Critic's own `rejected` push-backs."""
    store = InMemoryScreeningStore()
    thread_id = await screening.create_screening(store, "p.md", b"x")
    graph = ResumeGraph(updates=[], after=FakeSnapshot())

    await _reject(store, graph, thread_id, "Eligibility section is a placeholder.")

    assert graph.recorded is not None
    (audit_event,) = graph.recorded["events"]
    assert audit_event["agent"] == "human"
    assert audit_event["status"] == "rejected"
    assert REVIEWER.email in audit_event["detail"]
    assert "Eligibility section is a placeholder." in audit_event["detail"]


async def test_reject_denormalizes_the_store_row_to_rejected():
    """The bug this fixes: a walked-away-from run sat in `awaiting_approval`
    forever and was counted as in flight. The row has to say `rejected` — and keep
    the criteria count the run had earned."""
    store = InMemoryScreeningStore()
    thread_id = await screening.create_screening(store, "p.md", b"x")
    graph = ResumeGraph(updates=[], after=FakeSnapshot(), values={"parsed_criteria": _criteria()})

    await _reject(store, graph, thread_id)

    row = await _first_row(store)
    assert row.status == "rejected"
    assert row.criteria_count == 1
    # The matcher never ran, so there is no cohort to count.
    assert row.match_count == 0


async def test_reject_of_an_escalated_run_needs_no_cursor_move():
    """An escalated run has already reached END, so the values merge into the
    checkpoint where it stopped — posing as the matcher there would rewrite a node
    that never ran."""
    store = InMemoryScreeningStore()
    thread_id = await screening.create_screening(store, "p.md", b"x")
    graph = ResumeGraph(
        updates=[], after=FakeSnapshot(), gate=(), values={"current_step": "escalated"}
    )

    await _reject(store, graph, thread_id)

    assert graph.recorded_as_node is None
    assert (await _first_row(store)).status == "rejected"


@pytest.mark.parametrize("current_step", ["done", "failed", "parsing"])
async def test_reject_of_a_run_that_is_not_at_a_decision_point_is_409(current_step):
    """Rejecting a finished run would overwrite the cohort someone approved;
    rejecting a failed one would relabel a breakdown as a decision. Neither is a
    thing a reviewer is being asked about."""
    store = InMemoryScreeningStore()
    thread_id = await screening.create_screening(store, "p.md", b"x")
    graph = ResumeGraph(
        updates=[], after=FakeSnapshot(), gate=(), values={"current_step": current_step}
    )
    with pytest.raises(ScreeningNotRejectableError):
        await _reject(store, graph, thread_id)
    # Nothing recorded: the pre-check runs before the write, exactly as approval's
    # does, so a refused rejection leaves no trace claiming someone stopped the run.
    assert graph.recorded is None


async def test_reject_of_a_run_parked_before_the_critic_is_refused():
    """Pending, but not at this gate: an edit-and-rerun (#53) whose client vanished
    is parked at the Critic. Rejecting there would pose as the matcher for a run
    that never reached it — and the graph would still have a Critic pass to make."""
    store = InMemoryScreeningStore()
    thread_id = await screening.create_screening(store, "p.md", b"x")
    graph = ResumeGraph(updates=[], after=FakeSnapshot(), gate=("critic",))
    with pytest.raises(ScreeningNotRejectableError):
        await _reject(store, graph, thread_id)
    assert graph.recorded is None


async def test_reject_of_an_unknown_thread_is_404():
    store = InMemoryScreeningStore()
    with pytest.raises(ScreeningNotFoundError):
        await _reject(store, ResumeGraph(updates=[], after=FakeSnapshot()), "nope")


async def test_reject_counts_its_own_terminal_outcome():
    """No node runs when a reviewer stops a screening, so nothing would otherwise
    increment the funnel — and the run would vanish from it entirely (#91)."""
    store = InMemoryScreeningStore()
    thread_id = await screening.create_screening(store, "p.md", b"x")
    graph = ResumeGraph(updates=[], after=FakeSnapshot())
    before = _outcome_count("rejected")

    await _reject(store, graph, thread_id)

    assert _outcome_count("rejected") == before + 1


def _outcome_count(outcome: str) -> float:
    """The current `screenings_total{outcome=...}` value, read off the live
    collector the exposition endpoint serializes."""
    return sum(
        sample.value
        for family in metrics.screenings_total.collect()
        for sample in family.samples
        if sample.name == "screenings_total" and sample.labels.get("outcome") == outcome
    )


# --- edit and re-run (#53) -----------------------------------------------


def _criteria(value: float = 18, source: str = "Age 18 or older.") -> dict:
    return {
        "trial_title": "T",
        "inclusion_quantitative": [
            {
                "attribute": "age",
                "operator": ">=",
                "value": value,
                "value_high": None,
                "unit": "years",
                "source_text": source,
            }
        ],
        "inclusion_categorical": [],
        "exclusion_quantitative": [],
        "exclusion_categorical": [],
        "unparseable": [],
    }


def _parked(
    criteria: dict | None = None, revision: int = 0, step: str = "awaiting_approval"
) -> dict:
    return {
        "parsed_criteria": criteria if criteria is not None else _criteria(),
        "criteria_revision": revision,
        "current_step": step,
    }


async def _rerun(
    store: InMemoryScreeningStore,
    graph: screening.ScreeningGraph,
    thread_id: str,
    criteria: dict,
    base_revision: int = 0,
) -> list[dict]:
    frames = await _drain(
        await screening.resume_with_edited_criteria(
            store,
            _audit(),
            _rules(),
            graph,
            thread_id,
            criteria=criteria,
            base_revision=base_revision,
            editor=REVIEWER,
        )
    )
    return _frames(frames)


async def test_edit_writes_the_criteria_as_the_parser_and_streams_the_rerun():
    """The write has to pose as the *parser* — that is what rewinds the cursor so
    the Critic re-runs over the edited criteria instead of the run resuming
    straight into the matcher with compliance unchecked."""
    store = InMemoryScreeningStore()
    thread_id = await screening.create_screening(store, "p.md", b"x")
    graph = ResumeGraph(
        updates=[{"critic": {"current_step": "awaiting_approval"}}],
        after=FakeSnapshot(values={"current_step": "awaiting_approval"}, pending=("matcher",)),
        values=_parked(),
    )
    frames = await _rerun(store, graph, thread_id, _criteria(value=65))

    assert graph.recorded_as_node == "parser"
    assert graph.recorded_before_resume is True
    assert graph.recorded is not None
    assert graph.recorded["parsed_criteria"] == _criteria(value=65)
    assert graph.recorded["criteria_revision"] == 1
    # The Critic's verdict on the extraction that was just replaced must not
    # survive: a stale `passed` would route straight back to the gate.
    assert graph.recorded["compliance_passed"] is False
    assert graph.recorded["critic_feedback"] is None
    assert graph.recorded["current_step"] == "critiquing"
    # It re-parks at the gate, so the reviewer still has to approve matching.
    assert frames[-1] == {"node": sse.INTERRUPT}
    assert (await _first_row(store)).status == "awaiting_approval"


async def test_edit_records_the_editor_and_the_diff():
    store = InMemoryScreeningStore()
    thread_id = await screening.create_screening(store, "p.md", b"x")
    graph = ResumeGraph(
        updates=[],
        after=FakeSnapshot(values={"current_step": "awaiting_approval"}, pending=("matcher",)),
        values=_parked(),
    )
    await _rerun(store, graph, thread_id, _criteria(value=65))

    assert graph.recorded is not None
    # Appended, not replaced — `criteria_edits` carries the same reducer `events`
    # does, so revision 2's diff has to join revision 1's rather than erase it.
    (record,) = graph.recorded["criteria_edits"]
    assert record["revision"] == 1
    assert record["edited_by"] == REVIEWER.email
    assert record["edited_by_role"] == REVIEWER.role
    (change,) = record["changes"]
    assert (change["kind"], change["before"], change["after"]) == (
        "modified",
        "age >= 18 years",
        "age >= 65 years",
    )
    (audit_event,) = graph.recorded["events"]
    assert (audit_event["agent"], audit_event["status"]) == ("human", "edited")
    assert REVIEWER.email in audit_event["detail"]
    assert "1 modified" in audit_event["detail"]


async def test_edit_of_an_escalated_run_is_allowed():
    """The escalation path is exactly what this feature exists for: the graph gave
    up, so there is no pending node to resume — only a human edit moves it."""
    store = InMemoryScreeningStore()
    thread_id = await screening.create_screening(store, "p.md", b"x")
    graph = ResumeGraph(
        updates=[{"critic": {"current_step": "awaiting_approval"}}],
        after=FakeSnapshot(values={"current_step": "awaiting_approval"}, pending=("matcher",)),
        gate=(),  # nothing pending — the run ended at human_escalation
        values=_parked(step="escalated"),
    )
    frames = await _rerun(store, graph, thread_id, _criteria(value=65))
    assert graph.recorded_as_node == "parser"
    assert frames[-1] == {"node": sse.INTERRUPT}


async def test_edit_of_a_finished_run_is_allowed_and_discards_its_cohort():
    """Promoting a what-if (#95) means editing a run that has already scored.

    A finished run was frozen until then. It is editable now — the simulator's
    whole output is a threshold worth re-running — but the cohort it scored under
    the old criteria must not survive into a run that no longer has them: the run
    detail view would show revision 1's verdicts beneath revision 2's criteria, and
    the runs index would keep counting matches nobody approved.
    """
    store = InMemoryScreeningStore()
    thread_id = await screening.create_screening(store, "p.md", b"x")
    graph = ResumeGraph(
        updates=[],
        after=FakeSnapshot(values={"current_step": "awaiting_approval"}, pending=("matcher",)),
        gate=(),
        values={
            **_parked(step="done"),
            "matched_patients": [{"patient_id": "PT-1", "eligible": True, "needs_review": False}],
            "match_summary": "Screened 1 patient: 1 matching this protocol.",
        },
    )
    frames = await _rerun(store, graph, thread_id, _criteria(value=65))

    assert graph.recorded_as_node == "parser"
    assert graph.recorded is not None
    assert graph.recorded["matched_patients"] == []
    assert graph.recorded["match_summary"] is None
    assert graph.recorded["criteria_revision"] == 1
    # Both events, in order: what the reviewer did, then what it cost.
    details = [e["detail"] for e in graph.recorded["events"]]
    assert "Criteria revised by" in details[0]
    assert "Discarded the previous cohort of 1 scored patients" in details[1]
    # And it re-enters at the Critic like any other edit — no cohort until someone
    # approves the gate again.
    assert frames[-1] == {"node": sse.INTERRUPT}


async def test_edit_at_the_gate_does_not_mention_a_cohort_it_never_had():
    """The common path is untouched: a run parked before the Matcher has nothing
    to discard, so it gets no cohort keys and no second event."""
    store = InMemoryScreeningStore()
    thread_id = await screening.create_screening(store, "p.md", b"x")
    graph = ResumeGraph(updates=[], after=FakeSnapshot(), values=_parked())
    await _rerun(store, graph, thread_id, _criteria(value=65))

    assert graph.recorded is not None
    assert "matched_patients" not in graph.recorded
    assert "match_summary" not in graph.recorded
    assert len(graph.recorded["events"]) == 1


async def test_edit_of_a_rejected_run_still_raises():
    """The one terminal state editing must not reopen (#91): a rejection is a
    decision, and editing past it would erase it rather than reverse it."""
    store = InMemoryScreeningStore()
    thread_id = await screening.create_screening(store, "p.md", b"x")
    graph = ResumeGraph(updates=[], after=FakeSnapshot(), gate=(), values=_parked(step="rejected"))
    with pytest.raises(ScreeningNotEditableError):
        await screening.resume_with_edited_criteria(
            store,
            _audit(),
            _rules(),
            graph,
            thread_id,
            criteria=_criteria(65),
            base_revision=0,
            editor=REVIEWER,
        )
    assert graph.recorded is None


async def test_edit_of_a_run_with_no_extraction_raises():
    """A screening that never got past the router has nothing to correct."""
    store = InMemoryScreeningStore()
    thread_id = await screening.create_screening(store, "p.md", b"x")
    graph = ResumeGraph(
        updates=[],
        after=FakeSnapshot(),
        gate=(),
        values={"current_step": "failed", "parsed_criteria": None},
    )
    with pytest.raises(ScreeningNotEditableError):
        await screening.resume_with_edited_criteria(
            store,
            _audit(),
            _rules(),
            graph,
            thread_id,
            criteria=_criteria(65),
            base_revision=0,
            editor=REVIEWER,
        )
    assert graph.recorded is None


async def test_edit_against_a_stale_revision_raises_and_writes_nothing():
    """Two reviewers on the same parked run: the second save must not silently
    discard the first's corrections."""
    store = InMemoryScreeningStore()
    thread_id = await screening.create_screening(store, "p.md", b"x")
    graph = ResumeGraph(
        updates=[],
        after=FakeSnapshot(),
        values=_parked(revision=2),
    )
    with pytest.raises(CriteriaRevisionConflictError, match="revision 2"):
        await screening.resume_with_edited_criteria(
            store,
            _audit(),
            _rules(),
            graph,
            thread_id,
            criteria=_criteria(65),
            base_revision=1,
            editor=REVIEWER,
        )
    assert graph.recorded is None


async def test_edit_of_a_checkpoint_without_a_revision_treats_it_as_zero():
    """A run parked before #53 shipped has no `criteria_revision` in its
    checkpoint; editing it must work, not 409 against a missing field."""
    store = InMemoryScreeningStore()
    thread_id = await screening.create_screening(store, "p.md", b"x")
    graph = ResumeGraph(
        updates=[],
        after=FakeSnapshot(values={"current_step": "awaiting_approval"}, pending=("matcher",)),
        values={"parsed_criteria": _criteria(), "current_step": "awaiting_approval"},
    )
    await _rerun(store, graph, thread_id, _criteria(value=65), base_revision=0)
    assert graph.recorded is not None
    assert graph.recorded["criteria_revision"] == 1


async def test_edit_unknown_thread_raises_before_writing():
    store = InMemoryScreeningStore()
    graph = ResumeGraph(updates=[], after=FakeSnapshot(), values=_parked())
    with pytest.raises(ScreeningNotFoundError):
        await screening.resume_with_edited_criteria(
            store,
            _audit(),
            _rules(),
            graph,
            "nope",
            criteria=_criteria(65),
            base_revision=0,
            editor=REVIEWER,
        )
    assert graph.recorded is None


async def test_edit_rerun_that_escalates_again_streams_the_escalation():
    """A reviewer's edit is not privileged: if the Critic still rejects it and the
    attempt cap is spent, the run escalates again.

    The terminal frame is `__end__`, not `__error__` — `human_escalation` sets
    `current_step="escalated"`, and only `"failed"` becomes an error frame. That
    distinction is the contract the UI reads: the escalation *node* frame is what
    tells it the re-run was blocked, and the run stays editable at
    `status="escalated"` rather than being marked failed."""
    store = InMemoryScreeningStore()
    thread_id = await screening.create_screening(store, "p.md", b"x")
    graph = ResumeGraph(
        updates=[{"human_escalation": {"current_step": "escalated"}}],
        after=FakeSnapshot(values={"current_step": "escalated"}),
        values=_parked(step="escalated"),
    )
    frames = await _rerun(store, graph, thread_id, _criteria(value=65))
    assert [f["node"] for f in frames] == ["human_escalation", sse.END]
    assert (await _first_row(store)).status == "escalated"


# --- notify on gate / escalation (#60) -----------------------------------
#
# Delivery (webhook/SMTP, payload shape, PHI hygiene, failure isolation) is
# covered in test_notifications.py. What matters here is the *wiring*: the hook
# sits in the one place every operation funnels through, so all three streaming
# paths notify, and it is handed the same status the store row got.


@pytest.fixture
def notified(monkeypatch):
    """Capture `notify_gate` calls made by the service layer."""
    calls: list[dict] = []

    async def record(_settings: object, **kwargs: object) -> None:
        calls.append(dict(kwargs))

    monkeypatch.setattr(screening.notifications, "notify_gate", record)
    return calls


async def test_a_run_parking_at_the_gate_notifies(notified):
    store = InMemoryScreeningStore()
    thread_id = await screening.create_screening(store, "nsclc.md", b"x")
    graph = StreamingGraph(
        updates=[{"critic": {"current_step": "awaiting_approval"}}],
        snapshot=FakeSnapshot(
            values={
                "current_step": "awaiting_approval",
                "source_filename": "nsclc.md",
                "parsed_criteria": {"inclusion_quantitative": [{"attribute": "age"}]},
            },
            pending=("matcher",),
        ),
    )
    await _drain(await screening.stream_screening(store, _audit(), _rules(), graph, thread_id))

    (call,) = notified
    assert call["thread_id"] == thread_id
    # The same status the runs index shows — both come from _record_outcome.
    assert call["status"] == (await _first_row(store)).status == "awaiting_approval"
    assert call["source_filename"] == "nsclc.md"
    assert call["criteria_count"] == 1


async def test_an_escalated_run_notifies(notified):
    store = InMemoryScreeningStore()
    thread_id = await screening.create_screening(store, "p.md", b"x")
    graph = StreamingGraph(
        updates=[{"human_escalation": {"current_step": "escalated"}}],
        snapshot=FakeSnapshot(values={"current_step": "escalated", "source_filename": "p.md"}),
    )
    await _drain(await screening.stream_screening(store, _audit(), _rules(), graph, thread_id))

    (call,) = notified
    assert call["status"] == "escalated"


async def test_an_edit_rerun_that_re_parks_notifies_again(notified):
    """Each stop is a fresh ask: a reviewer's edit that lands back at the gate has
    to page whoever approves, not rely on them still watching the stream."""
    store = InMemoryScreeningStore()
    thread_id = await screening.create_screening(store, "p.md", b"x")
    graph = ResumeGraph(
        updates=[{"critic": {"current_step": "awaiting_approval"}}],
        after=FakeSnapshot(values={"current_step": "awaiting_approval"}, pending=("matcher",)),
        values=_parked(),
    )
    await _rerun(store, graph, thread_id, _criteria(value=65))
    assert [c["status"] for c in notified] == ["awaiting_approval"]


async def test_a_finished_run_is_not_notified(notified):
    """The gating lives in notify_gate, but the approve path reaching it at all
    would mean a completed run pages a reviewer with nothing to do."""
    store = InMemoryScreeningStore()
    thread_id = await screening.create_screening(store, "p.md", b"x")
    graph = ResumeGraph(
        updates=[{"matcher": {"current_step": "done"}}],
        after=FakeSnapshot(values={"current_step": "done"}),
    )
    await _approve_frames(store, graph, thread_id)
    assert [c["status"] for c in notified] == ["done"]


async def test_a_crashed_run_is_not_notified(notified):
    """The error path bails before the outcome/notify block: a crashed stream is
    surfaced as an __error__ frame, and there is no snapshot to describe."""
    store = InMemoryScreeningStore()
    thread_id = await screening.create_screening(store, "p.md", b"x")
    await _drain(
        await screening.stream_screening(
            store, _audit(), _rules(), RaisingGraph(RuntimeError("boom")), thread_id
        )
    )
    assert notified == []


async def test_a_dead_notification_channel_cannot_fail_a_good_run(monkeypatch):
    """End-to-end on the property that makes this safe to put in the hot path: with
    the *real* notify_gate and a webhook that refuses the connection, the run still
    ends at __interrupt__ and stays approvable.

    The hook is inside `_graph_frames`' try block, so anything escaping it would
    turn a successful screening into an __error__ frame + status="failed".
    """
    store = InMemoryScreeningStore()
    thread_id = await screening.create_screening(store, "p.md", b"x")

    settings = Settings(
        _env_file=None,
        notify_enabled=True,
        notify_webhook_url="https://hooks.invalid/nope",
        notify_timeout_seconds=0.1,
    )
    monkeypatch.setattr(screening, "get_settings", lambda: settings)

    class Refusing:
        async def __aenter__(self) -> "Refusing":
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def post(self, *_a: object, **_k: object) -> None:
            raise RuntimeError("connection refused")

    monkeypatch.setattr(notifications.httpx, "AsyncClient", lambda **_kw: Refusing())

    graph = StreamingGraph(
        updates=[],
        snapshot=FakeSnapshot(values={"current_step": "awaiting_approval"}, pending=("matcher",)),
    )
    frames = _frames(
        await _drain(await screening.stream_screening(store, _audit(), _rules(), graph, thread_id))
    )
    assert frames[-1] == {"node": sse.INTERRUPT}
    assert (await _first_row(store)).status == "awaiting_approval"


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
