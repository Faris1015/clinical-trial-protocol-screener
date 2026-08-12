"""The org-wide audit log of human decisions (#98).

Acceptance criteria exercised here, in order:

1. `GET /api/audit` returns approvals, rejections, criteria revisions and
   escalations across all runs, newest first, paginated.
2. Filterable by actor, action type, date range and run.
3. Each entry carries the run it belongs to, and a revision carries the number
   that addresses its before/after diff.
4. The index is populated as decisions happen — asserted by driving each of the
   four decision paths and reading the index, never by scanning checkpoints.
5. PHI-safe by construction, asserted against a run with a scored cohort.
6. Exportable as CSV and JSON.
7. Visible to `admin`; a `reviewer` sees their own decisions.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

import app.graph.nodes.critic as critic_mod
import app.graph.nodes.matcher as matcher_mod
import app.graph.nodes.parser as parser_mod
import app.main as main
from app.auth import Principal
from app.config import Settings
from app.exceptions import (
    AuditExportTooLargeError,
    AuthorizationDeniedError,
    InvalidAuditFilterError,
)
from app.persistence import (
    AuditDecision,
    AuditFilter,
    InMemoryAuditStore,
    InMemoryRuleStore,
    InMemoryScreeningStore,
    open_persistence,
)
from app.services import audit, screening
from tests.auth_helpers import ADMIN, REVIEWER, sign_in
from tests.fakes import FAKE_PATIENTS, PROTOCOL_TEXT, FakeChatModel, good_criteria

OTHER_REVIEWER = Principal(email="second@test.local", role="reviewer")


def _decision(
    *,
    thread_id: str = "run-1",
    action: str = "approved",
    actor: str = REVIEWER.email,
    actor_role: str = "reviewer",
    at: str = "2026-07-01T09:00:00+00:00",
    detail: str = "Cleared the approval gate",
    revision: int = 0,
    source_filename: str = "protocol.md",
) -> AuditDecision:
    return AuditDecision(
        thread_id=thread_id,
        action=action,
        actor=actor,
        actor_role=actor_role,
        occurred_at=at,
        detail=detail,
        revision=revision,
        source_filename=source_filename,
    )


async def _seeded() -> InMemoryAuditStore:
    """Four decisions, two actors, three runs, across three days."""
    store = InMemoryAuditStore()
    await store.record(_decision(thread_id="run-a", at="2026-07-01T09:00:00+00:00"))
    await store.record(
        _decision(
            thread_id="run-b",
            action="rejected",
            at="2026-07-02T09:00:00+00:00",
            actor=OTHER_REVIEWER.email,
            detail="Rejected the protocol as unscreenable — not oncology",
        )
    )
    await store.record(
        _decision(
            thread_id="run-a",
            action="criteria_revised",
            at="2026-07-03T09:00:00+00:00",
            revision=2,
        )
    )
    await store.record(
        _decision(
            thread_id="run-c",
            action="escalated",
            actor=audit.SYSTEM_ACTOR,
            actor_role=audit.SYSTEM_ROLE,
            at="2026-07-03T18:00:00+00:00",
            detail="Escalated for human review after 3 parse attempts",
        )
    )
    return store


# --- The index (AC 1) -------------------------------------------------------


async def test_the_index_returns_every_decision_newest_first():
    store = await _seeded()
    page = await store.list(limit=10, offset=0)

    assert page.total == 4
    assert [row.action for row in page.items] == [
        "escalated",
        "criteria_revised",
        "rejected",
        "approved",
    ]


async def test_decisions_sharing_a_timestamp_keep_a_total_order():
    """Two reviewers acting in the same microsecond still have to page cleanly:
    without the sequence number as a tiebreaker the two rows could swap between
    the request for page 1 and the request for page 2, dropping one and repeating
    the other."""
    store = InMemoryAuditStore()
    same = "2026-07-04T09:00:00+00:00"
    for thread_id in ("run-1", "run-2", "run-3"):
        await store.record(_decision(thread_id=thread_id, at=same))

    first = await store.list(limit=2, offset=0)
    second = await store.list(limit=2, offset=2)
    assert [row.id for row in first.items] == [3, 2]
    assert [row.id for row in second.items] == [1]


async def test_paging_reports_the_total_the_filter_matched_not_the_page():
    store = await _seeded()
    page = await store.list(limit=2, offset=0)
    assert len(page.items) == 2
    assert page.total == 4


# --- Filters (AC 2) ---------------------------------------------------------


async def test_filter_by_actor_action_and_run():
    store = await _seeded()

    by_actor = await store.list(limit=10, offset=0, filters=AuditFilter(actor=REVIEWER.email))
    assert {row.thread_id for row in by_actor.items} == {"run-a"}

    by_action = await store.list(limit=10, offset=0, filters=AuditFilter(action="rejected"))
    assert [row.thread_id for row in by_action.items] == ["run-b"]

    by_run = await store.list(limit=10, offset=0, filters=AuditFilter(thread_id="run-a"))
    assert {row.action for row in by_run.items} == {"approved", "criteria_revised"}


async def test_filters_combine_with_and():
    store = await _seeded()
    page = await store.list(
        limit=10,
        offset=0,
        filters=AuditFilter(actor=REVIEWER.email, action="criteria_revised"),
    )
    assert [row.revision for row in page.items] == [2]


async def test_an_actor_filter_is_an_exact_match_not_a_substring():
    """A prefix match would leak: a reviewer scoped to `ana@…` must not be able to
    read `susana@…`'s decisions by way of a LIKE."""
    store = InMemoryAuditStore()
    await store.record(_decision(actor="susana@test.local"))
    page = await store.list(limit=10, offset=0, filters=AuditFilter(actor="ana@test.local"))
    assert page.total == 0


async def test_a_bare_date_range_covers_the_whole_of_both_days():
    store = await _seeded()
    criteria = audit.build_filter(
        actor=None, action=None, thread_id=None, since="2026-07-02", until="2026-07-03"
    )
    page = await store.list(limit=10, offset=0, filters=criteria)
    # Including the escalation at 18:00 on the 3rd — an upper bound at midnight
    # would silently drop every decision made during the day the caller named.
    assert [row.action for row in page.items] == ["escalated", "criteria_revised", "rejected"]


def test_parse_bound_normalizes_days_and_instants_to_utc():
    assert audit.parse_bound("2026-07-02", upper=False) == "2026-07-02T00:00:00+00:00"
    assert audit.parse_bound("2026-07-02", upper=True) == "2026-07-02T23:59:59.999999+00:00"
    # A naive instant is read as UTC — the offset every stored stamp is written in.
    assert audit.parse_bound("2026-07-02T08:30:00", upper=False) == "2026-07-02T08:30:00+00:00"
    # An offset-carrying instant is converted rather than trusted as written.
    assert audit.parse_bound("2026-07-02T08:30:00+02:00", upper=True) == "2026-07-02T06:30:00+00:00"


def test_an_unreadable_filter_is_refused_rather_than_dropped():
    """Ignoring a filter returns a page wider than the one asked for, and an
    auditor reading it would take an unscoped answer for a scoped one."""
    with pytest.raises(InvalidAuditFilterError):
        audit.parse_bound("last tuesday", upper=False)
    with pytest.raises(InvalidAuditFilterError):
        audit.build_filter(actor=None, action="deleted", thread_id=None, since=None, until=None)
    with pytest.raises(InvalidAuditFilterError):
        audit.build_filter(
            actor=None, action=None, thread_id=None, since="2026-07-05", until="2026-07-01"
        )


# --- Scope (AC 7) -----------------------------------------------------------


def test_an_admin_reads_the_whole_index_and_may_narrow_it_to_anyone():
    assert audit.visible_actor(ADMIN, None) is None
    assert audit.visible_actor(ADMIN, REVIEWER.email) == REVIEWER.email


def test_a_reviewer_is_scoped_to_their_own_decisions():
    assert audit.visible_actor(REVIEWER, None) == REVIEWER.email
    assert audit.visible_actor(REVIEWER, REVIEWER.email.upper()) == REVIEWER.email


def test_a_reviewer_asking_for_someone_elses_decisions_is_refused():
    """A 403 rather than an empty page: "this person did nothing" and "you may not
    ask" are different answers, and an empty result blurs them."""
    with pytest.raises(AuthorizationDeniedError):
        audit.visible_actor(REVIEWER, ADMIN.email)


async def test_the_scope_is_applied_in_the_query_not_over_the_page():
    """A reviewer's own decision count is what they get back, and the envelope
    says which scope produced it."""
    store = await _seeded()
    payload = await audit.list_decisions(store, REVIEWER, limit=10, offset=0)
    assert payload["total"] == 2
    assert {item["thread_id"] for item in payload["items"]} == {"run-a"}
    assert payload["scope"]["actor"] == REVIEWER.email

    everyones = await audit.list_decisions(store, ADMIN, limit=10, offset=0)
    assert everyones["total"] == 4
    assert everyones["scope"]["actor"] is None


async def test_an_escalation_belongs_to_no_reviewer():
    """The Critic gave up; naming whichever reviewer later picks the run up would
    put someone's name on a decision they did not make. So it is the pipeline's,
    and therefore admin-visible."""
    store = await _seeded()
    mine = await audit.list_decisions(store, REVIEWER, limit=10, offset=0)
    assert "escalated" not in {item["action"] for item in mine["items"]}

    everyones = await audit.list_decisions(store, ADMIN, limit=10, offset=0, action="escalated")
    (entry,) = everyones["items"]
    assert entry["actor"] == audit.SYSTEM_ACTOR
    assert entry["actor_role"] == audit.SYSTEM_ROLE


# --- Recording as decisions happen (AC 4) -----------------------------------


class _Snapshot:
    def __init__(self, values: dict | None = None, pending: tuple = ()):
        self.values = values or {}
        self.next = pending


class _Graph:
    """A graph parked wherever `pending` says, ending in `after`.

    Enough of `ScreeningGraph` to drive the four decision paths: the pre-checks
    read `aget_state`, the decision writes through `aupdate_state`, and the resume
    streams nothing before landing on the terminal snapshot.
    """

    def __init__(
        self,
        *,
        pending: tuple = ("matcher",),
        values: dict | None = None,
        after: _Snapshot | None = None,
    ):
        self.snapshot = _Snapshot(values or {}, pending)
        self.after = after or _Snapshot({"current_step": "done"})
        self._read = 0

    async def astream(self, *_a: object, **_k: object) -> AsyncIterator[dict]:
        return
        yield {}  # pragma: no cover - makes this an async generator

    async def aget_state(self, _config: object) -> _Snapshot:
        self._read += 1
        return self.snapshot if self._read == 1 else self.after

    async def aupdate_state(
        self, _config: object, _values: dict, as_node: str | None = None
    ) -> None:
        return None

    async def ainvoke(self, *_a: object) -> dict:  # pragma: no cover - not driven here
        raise NotImplementedError


CRITERIA = {
    "trial_title": "Trial",
    "inclusion_quantitative": [
        {
            "attribute": "age",
            "operator": ">=",
            "value": 18.0,
            "value_high": None,
            "unit": "years",
            "source_text": "aged 18 years or older",
        }
    ],
    "inclusion_categorical": [],
    "exclusion_quantitative": [],
    "exclusion_categorical": [],
    "unparseable": [],
}


def _revised(age: float) -> dict:
    edited: dict = json.loads(json.dumps(CRITERIA))
    edited["inclusion_quantitative"][0]["value"] = age
    return edited


async def _fixture() -> tuple[InMemoryScreeningStore, InMemoryAuditStore, str]:
    store = InMemoryScreeningStore()
    index = InMemoryAuditStore()
    thread_id = await screening.create_screening(store, "protocol.md", b"x")
    return store, index, thread_id


async def _rows(index: InMemoryAuditStore) -> list:
    return (await index.list(limit=10, offset=0)).items


async def test_an_approval_is_indexed_with_the_stamp_the_checkpoint_carries():
    store, index, thread_id = await _fixture()
    graph = _Graph(values={"source_filename": "protocol.md"})

    frames = await screening.approve_screening(store, index, graph, thread_id, REVIEWER)
    async for _frame in frames:
        pass

    (row,) = await _rows(index)
    assert row.action == "approved"
    assert row.actor == REVIEWER.email
    assert row.actor_role == "reviewer"
    assert row.thread_id == thread_id
    assert row.source_filename == "protocol.md"
    # Parses as an instant — the stamp written into the checkpoint, not a
    # re-derivation a round trip later.
    assert datetime.fromisoformat(row.occurred_at).tzinfo is not None


async def test_an_approval_that_is_refused_indexes_nothing():
    """The 409 pre-check runs first, so the index never claims someone authorized
    a run that did not resume."""
    store, index, thread_id = await _fixture()
    graph = _Graph(pending=())
    with pytest.raises(Exception):  # noqa: B017 - ScreeningNotApprovableError
        await screening.approve_screening(store, index, graph, thread_id, REVIEWER)
    assert await _rows(index) == []


async def test_a_rejection_is_indexed_with_its_reason():
    """A rejection is the one decision whose *why* is the record — an entry saying
    only "rejected" would send the reader back to the run to learn what this
    endpoint already knew."""
    store, index, thread_id = await _fixture()
    graph = _Graph(values={"source_filename": "protocol.md"})

    await screening.reject_screening(
        store, index, graph, thread_id, OTHER_REVIEWER, "Not an oncology protocol."
    )

    (row,) = await _rows(index)
    assert row.action == "rejected"
    assert row.actor == OTHER_REVIEWER.email
    assert "Not an oncology protocol." in row.detail


async def test_a_criteria_revision_is_indexed_with_the_revision_it_produced():
    """AC 3: the number is what addresses the before/after diff in the run's own
    checkpoint, so an entry links to *this* revision rather than to the run."""
    store, index, thread_id = await _fixture()
    graph = _Graph(
        values={
            "parsed_criteria": CRITERIA,
            "criteria_revision": 0,
            "current_step": "awaiting_approval",
            "source_filename": "protocol.md",
        }
    )

    frames = await screening.resume_with_edited_criteria(
        store,
        index,
        InMemoryRuleStore(),
        graph,
        thread_id,
        criteria=_revised(65),
        base_revision=0,
        editor=REVIEWER,
    )
    async for _frame in frames:
        pass

    rows = await _rows(index)
    (revision,) = [row for row in rows if row.action == "criteria_revised"]
    assert revision.revision == 1
    # Counted, not listed — the same reading `criteria_edits.summarize` gives the
    # run's own event log; the full before/after lives in the checkpoint.
    assert "1 modified" in revision.detail


async def test_an_escalation_is_indexed_when_the_run_reaches_it():
    store, index, thread_id = await _fixture()
    graph = _Graph(pending=(), values={"current_step": "escalated", "parse_attempts": 3})

    frames = await screening.stream_screening(store, index, InMemoryRuleStore(), graph, thread_id)
    async for _frame in frames:
        pass

    (row,) = await _rows(index)
    assert row.action == "escalated"
    assert row.actor == audit.SYSTEM_ACTOR
    assert "3 parse attempts" in row.detail


async def test_a_run_that_simply_finishes_indexes_nothing():
    """The index carries decisions, not phases: a run reaching `done` on its own is
    the pipeline working, and an entry for it would bury the four that matter."""
    store, index, thread_id = await _fixture()
    graph = _Graph(pending=(), after=_Snapshot({"current_step": "done"}))

    frames = await screening.stream_screening(store, index, InMemoryRuleStore(), graph, thread_id)
    async for _frame in frames:
        pass

    assert await _rows(index) == []


class _BrokenIndex(InMemoryAuditStore):
    async def record(self, decision: AuditDecision) -> None:
        raise RuntimeError("the index is down")


async def test_a_failed_index_write_does_not_undo_a_made_decision():
    """The checkpoint is the record and is already written by this point; raising
    here would fail an approval that has already happened and ask the client to
    retry something the graph has done."""
    store, _index, thread_id = await _fixture()
    graph = _Graph(values={"source_filename": "protocol.md"})

    frames = await screening.approve_screening(store, _BrokenIndex(), graph, thread_id, REVIEWER)
    assert [frame async for frame in frames]


# --- PHI safety (AC 5) ------------------------------------------------------


@pytest.fixture
def offline(monkeypatch):
    """A screening that runs to completion with no network and no model."""
    monkeypatch.setattr(parser_mod, "get_llm", lambda: FakeChatModel([good_criteria()]))
    monkeypatch.setattr(critic_mod, "run_llm_semantic_review", lambda _state: [])
    monkeypatch.setattr(matcher_mod, "load_patients", lambda: FAKE_PATIENTS)


async def _screen_and_approve(client) -> str:
    upload = await client.post(
        "/api/screenings",
        files={"file": ("protocol.md", PROTOCOL_TEXT.encode(), "text/markdown")},
    )
    thread_id = str(upload.json()["thread_id"])
    async with client.stream("GET", f"/api/screenings/{thread_id}/stream") as resp:
        async for _line in resp.aiter_lines():
            pass
    async with client.stream("POST", f"/api/screenings/{thread_id}/approve", json={}) as resp:
        async for _line in resp.aiter_lines():
            pass
    return thread_id


async def test_the_index_carries_no_patient_data(offline):
    """Asserted against a run that actually scored a cohort, and asserted two ways:
    no patient's identity appears anywhere in the serialized index, and an entry's
    field set is exactly the closed vocabulary — so a field added later that could
    carry PHI fails this test rather than shipping."""
    async with main.lifespan(main.app):
        transport = ASGITransport(app=main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            sign_in(client, ADMIN)
            thread_id = await _screen_and_approve(client)
            state = (await client.get(f"/api/screenings/{thread_id}/state")).json()
            page = (await client.get("/api/audit")).json()

    # The run really did score patients, or this asserts nothing.
    assert state["values"]["matched_patients"]
    assert page["items"], "the approval was indexed"

    serialized = json.dumps(page)
    for patient in FAKE_PATIENTS:
        assert str(patient["id"]) not in serialized
        assert str(patient["name"]) not in serialized

    assert set(page["items"][0]) == {
        "id",
        "thread_id",
        "action",
        "label",
        "actor",
        "actor_role",
        "occurred_at",
        "detail",
        "revision",
        "source_filename",
        # What the decision was about (#97). PHI-safe by the same construction as
        # the rest: a closed kind enum, and an id that is either a server-minted
        # thread_id or an admin-authored rule id — neither has anywhere for a
        # patient to be.
        "subject_kind",
        "subject_id",
    }


# --- Export (AC 6) ----------------------------------------------------------


async def test_the_csv_export_names_its_columns_and_defuses_its_cells():
    store = InMemoryAuditStore()
    await store.record(
        _decision(
            action="rejected",
            # A reviewer's free text and an uploaded filename — both untrusted, and
            # both a formula to Excel if written through unchanged.
            detail="=cmd|'/c calc'!A1",
            source_filename="+protocol.md",
        )
    )
    _filename, body = await audit.export_decisions(store, ADMIN, "csv")

    assert body.startswith("﻿")
    header, *rows = body.removeprefix("﻿").strip().split("\r\n")
    assert header.split(",")[:5] == [
        "id",
        "occurred_at",
        "action",
        "action_label",
        "actor",
    ]
    assert "'=cmd" in rows[0]
    assert "'+protocol.md" in rows[0]


async def test_the_json_export_states_its_scope_and_its_counts():
    store = await _seeded()
    filename, body = await audit.export_decisions(
        store, ADMIN, "json", action="approved", generated_at=datetime(2026, 7, 4, tzinfo=UTC)
    )
    payload = json.loads(body)

    assert filename == "trialgate-audit-2026-07-04.json"
    assert payload["scope"]["action"] == "approved"
    # Both figures travel, always: `exported` under `total` is the only way a
    # truncated file can say so in its own body.
    assert payload["total"] == payload["exported"] == 1
    assert payload["decisions"][0]["label"] == "Approved"


async def test_an_export_too_large_to_be_complete_is_refused():
    """A CSV has nowhere to say it holds only the newest N rows, so a short one is
    indistinguishable from the whole answer. Refusing names the count and points at
    the way through; truncating would hand an auditor a file they cannot trust."""
    store = InMemoryAuditStore()
    for index in range(audit.MAX_EXPORT_ROWS + 1):
        await store.record(_decision(thread_id=f"run-{index}"))

    with pytest.raises(AuditExportTooLargeError) as refused:
        await audit.export_decisions(store, ADMIN, "csv")
    assert str(audit.MAX_EXPORT_ROWS + 1) in str(refused.value)

    # And a filter that brings it back under the cap goes through, so the message's
    # advice is advice a caller can act on.
    _filename, body = await audit.export_decisions(store, ADMIN, "csv", thread_id="run-1")
    assert body.count("\r\n") == 2  # header + the one row


async def test_an_export_at_exactly_the_cap_is_served():
    """The boundary is inclusive: the cap is what one file carries, not one less."""
    store = InMemoryAuditStore()
    for index in range(audit.MAX_EXPORT_ROWS):
        await store.record(_decision(thread_id=f"run-{index}"))
    _filename, body = await audit.export_decisions(store, ADMIN, "json")
    payload = json.loads(body)
    assert payload["total"] == payload["exported"] == audit.MAX_EXPORT_ROWS


async def test_an_export_is_scoped_the_way_the_index_is():
    """A reviewer must not be able to download what they cannot read."""
    store = await _seeded()
    _filename, body = await audit.export_decisions(store, REVIEWER, "json")
    payload = json.loads(body)
    assert payload["total"] == 2
    assert {row["actor"] for row in payload["decisions"]} == {REVIEWER.email}


# --- The HTTP edge ----------------------------------------------------------


@pytest.fixture
def client():
    with TestClient(main.app) as test_client:
        sign_in(test_client, ADMIN)
        yield test_client


def test_the_index_route_answers_the_runs_index_envelope(client):
    body = client.get("/api/audit").json()
    assert set(body) == {"items", "total", "limit", "offset", "scope"}
    assert body["limit"] == main.DEFAULT_PAGE_SIZE


def test_an_unknown_action_filter_is_a_422(client):
    response = client.get("/api/audit", params={"action": "deleted"})
    assert response.status_code == 422
    assert "criteria_revised" in response.json()["detail"]


def test_an_unreadable_date_filter_is_a_422(client):
    assert client.get("/api/audit", params={"from": "yesterday"}).status_code == 422


def test_a_reviewer_may_not_read_another_actors_decisions_over_http(client):
    sign_in(client, REVIEWER)
    response = client.get("/api/audit", params={"actor": ADMIN.email})
    assert response.status_code == 403


def test_the_page_size_ceiling_is_enforced(client):
    assert client.get("/api/audit", params={"limit": 10_000}).status_code == 422


def test_the_export_route_serves_an_attachment_that_cannot_render_as_a_page(client):
    response = client.get("/api/audit/export", params={"format": "csv"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert response.headers["content-disposition"].startswith("attachment; filename=")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == "default-src 'none'"


def test_an_instant_filter_is_honored_as_written(client):
    """The app's own filter sends instants rather than bare days (see
    `frontend/src/lib/audit.dayBound`), so the endpoint has to take one."""
    response = client.get(
        "/api/audit", params={"from": "2026-07-01T04:00:00.000Z", "to": "2026-07-02T03:59:59.999Z"}
    )
    assert response.status_code == 200
    assert response.json()["scope"]["from"] == "2026-07-01T04:00:00+00:00"


def test_an_unknown_export_format_is_a_422(client):
    assert client.get("/api/audit/export", params={"format": "xlsx"}).status_code == 422


# --- Durability -------------------------------------------------------------


async def test_the_index_outlives_the_process(tmp_path):
    """The whole point of a written index: a decision made before a restart is
    still an answer to an auditor's question after one."""
    settings = Settings(
        _env_file=None, checkpoint_backend="sqlite", sqlite_path=tmp_path / "screenings.sqlite"
    )

    first = await open_persistence(settings)
    try:
        await audit.record(
            first.audit,
            "approved",
            "run-a",
            actor=REVIEWER,
            detail="Cleared the approval gate",
            source_filename="protocol.md",
        )
    finally:
        await first.aclose()

    second = await open_persistence(settings)
    try:
        page = await second.audit.list(limit=10, offset=0)
    finally:
        await second.aclose()

    (row,) = page.items
    assert row.actor == REVIEWER.email
    assert row.thread_id == "run-a"
    assert row.id == 1


async def test_setup_is_idempotent_over_an_existing_index(tmp_path):
    """`open_persistence` runs the DDL on every start, so an upgrade of a
    deployment with an existing database must not fail or lose rows."""
    settings = Settings(
        _env_file=None, checkpoint_backend="sqlite", sqlite_path=tmp_path / "screenings.sqlite"
    )
    for _ in range(2):
        persistence = await open_persistence(settings)
        try:
            await persistence.audit.record(_decision())
        finally:
            await persistence.aclose()

    persistence = await open_persistence(settings)
    try:
        assert (await persistence.audit.list(limit=10, offset=0)).total == 2
    finally:
        await persistence.aclose()
