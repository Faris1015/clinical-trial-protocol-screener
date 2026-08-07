"""The per-run event timeline and its route (#55).

Two halves: `app/services/timeline.py`, which turns the graph's append-only event
log into an audit trail (retry rounds numbered, Critic rejections named, human
steps attributed); and `GET /api/screenings/{id}/state`, which serves it beside
the checkpoint it was derived from.

The fixtures below are shaped like real logs — the details are copied from the
wording the nodes actually emit — because the whole point of the module is that a
checkpoint written months ago still reads as a story.
"""

from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.services import timeline
from tests.auth_helpers import sign_in

PROTOCOL = (
    "Phase II single-arm study of an investigational agent in adults.\n\n"
    "Inclusion criteria:\n"
    "- Age 18 years or older at the time of consent.\n\n"
    "Exclusion criteria:\n"
    "- Participation in another interventional trial within the prior 30 days.\n"
)


def _event(agent: str, status: str, detail: str, timestamp: str) -> dict[str, Any]:
    return {"agent": agent, "status": status, "detail": detail, "timestamp": timestamp}


# A run that looped once, was corrected by a reviewer, then approved and matched —
# every path #55 has to make legible, in one log.
FULL_LOG = [
    _event("router", "completed", "Admitted 'p.pdf' (940 chars)", "2026-07-30T13:59:00+00:00"),
    _event("parser", "completed", "Extracted 3/1 (attempt 1)", "2026-07-30T13:59:02+00:00"),
    _event("critic", "rejected", "2 findings (1 blocking)", "2026-07-30T13:59:03.500000+00:00"),
    _event("parser", "completed", "Extracted 4/1 (attempt 2)", "2026-07-30T13:59:05+00:00"),
    _event("critic", "completed", "1 findings (0 blocking)", "2026-07-30T13:59:06+00:00"),
    _event(
        "human",
        "edited",
        "Criteria revised by editor@test.local (reviewer) — 1 modified (revision 1)",
        "2026-07-30T14:00:00+00:00",
    ),
    _event("critic", "completed", "0 findings (0 blocking)", "2026-07-30T14:00:01+00:00"),
    _event(
        "human",
        "approved",
        "Approval gate cleared by boss@test.local (lead)",
        "2026-07-30T14:02:00+00:00",
    ),
    _event("matcher", "completed", "Screened 3 patients: 2 eligible", "2026-07-30T14:02:04+00:00"),
]


def _values(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "current_step": "done",
        "events": FULL_LOG,
        "criteria_revision": 1,
        "criteria_edits": [
            {
                "revision": 1,
                "edited_by": "editor@test.local",
                "edited_by_role": "reviewer",
                "edited_at": "2026-07-30T14:00:00+00:00",
                "changes": [],
            }
        ],
        "approved_by": "boss@test.local",
        "approved_by_role": "lead",
        "approved_at": "2026-07-30T14:02:00+00:00",
    }
    values.update(overrides)
    return values


def _entries(**overrides: Any) -> list[timeline.TimelineEntry]:
    return timeline.build_timeline(_values(**overrides))["entries"]


def _summary(**overrides: Any) -> timeline.TimelineSummary:
    return timeline.build_timeline(_values(**overrides))["summary"]


# --- Order and labelling ----------------------------------------------------


def test_the_timeline_is_the_event_log_in_the_order_it_happened():
    entries = _entries()

    assert [entry["seq"] for entry in entries] == list(range(len(FULL_LOG)))
    assert [entry["label"] for entry in entries] == [
        "Router",
        "Parser",
        "Regulatory Critic",
        "Parser",
        "Regulatory Critic",
        "Reviewer",
        "Regulatory Critic",
        "Reviewer",
        "Patient Matcher",
    ]
    # The detail the node wrote is carried through untouched — it is the only part
    # of a step this module does not rewrite.
    assert entries[0]["detail"] == "Admitted 'p.pdf' (940 chars)"


def test_each_step_carries_a_readable_outcome_beside_its_raw_status():
    outcomes = {(entry["status"], entry["outcome"]) for entry in _entries()}

    assert outcomes == {
        ("completed", "Completed"),
        ("rejected", "Rejected"),
        ("edited", "Edited"),
        ("approved", "Approved"),
    }


def test_an_agent_or_status_this_build_does_not_know_renders_as_itself():
    """A label map must never turn a future event into a blank row."""
    entries = _entries(
        events=[_event("summarizer", "throttled", "waited", "2026-07-30T13:59:00+00:00")]
    )

    assert entries[0]["label"] == "summarizer"
    assert entries[0]["outcome"] == "throttled"


# --- Retries and Critic rejections ------------------------------------------


def test_a_retry_round_is_numbered_the_way_the_parser_numbers_itself():
    """The derived attempt has to agree with the Parser's own "(attempt N)" text —
    two numbers for one round that disagreed would be worse than one."""
    parses = [entry for entry in _entries() if entry["agent"] == "parser"]

    assert [entry["attempt"] for entry in parses] == [1, 2]
    for entry in parses:
        assert f"(attempt {entry['attempt']})" in entry["detail"]


def test_a_critic_step_belongs_to_the_round_that_produced_what_it_reviewed():
    critiques = [entry for entry in _entries() if entry["agent"] == "critic"]

    # Rejected round 1, cleared round 2 — and the re-run after the reviewer's edit
    # is still round 2, because an edit does not spend an automated attempt.
    assert [(entry["status"], entry["attempt"]) for entry in critiques] == [
        ("rejected", 1),
        ("completed", 2),
        ("completed", 2),
    ]


def test_only_the_retry_loop_carries_an_attempt_number():
    """Numbering the Router or the gate would imply they can repeat."""
    outside = [entry for entry in _entries() if entry["agent"] in {"router", "matcher", "human"}]

    assert outside and all(entry["attempt"] == 0 for entry in outside)


def test_the_summary_counts_the_retries_and_the_rejections():
    summary = _summary()

    assert summary["attempts"] == 2
    assert summary["critic_rejections"] == 1
    assert summary["escalated"] is False


def test_a_parse_that_failed_still_counts_as_an_attempt():
    """`parse_attempts` in state only advances on a *successful* extraction, so a
    run whose last try died in the Parser has one more attempt than that counter."""
    summary = _summary(
        events=[
            _event("parser", "completed", "Extracted (attempt 1)", "2026-07-30T13:59:02+00:00"),
            _event("critic", "rejected", "1 findings (1 blocking)", "2026-07-30T13:59:03+00:00"),
            _event(
                "parser",
                "failed",
                "Model output failed schema validation twice",
                "2026-07-30T13:59:09+00:00",
            ),
        ]
    )

    assert summary["attempts"] == 2


def test_an_escalated_run_says_so():
    escalation = _event(
        "critic",
        "escalated",
        "Could not converge after 3 attempts — human review required",
        "2026-07-30T13:59:20+00:00",
    )
    trail = timeline.build_timeline(_values(events=[*FULL_LOG[:3], escalation]))

    assert trail["entries"][-1]["outcome"] == "Escalated"
    assert trail["summary"]["escalated"] is True


# --- Who did it -------------------------------------------------------------


def test_the_approval_step_names_the_reviewer_who_cleared_the_gate():
    approval = next(entry for entry in _entries() if entry["status"] == "approved")

    assert approval["actor"] == "boss@test.local"
    assert approval["actor_role"] == "lead"


def test_the_approver_is_read_from_the_durable_trail_not_the_event_text():
    """The identity comes from `approved_by` (#50), which is the record that has to
    survive — not from parsing the sentence the gate happened to log."""
    approval = next(
        entry
        for entry in _entries(events=[_event("human", "approved", "", "2026-07-30T14:02:00+00:00")])
        if entry["status"] == "approved"
    )

    assert approval["actor"] == "boss@test.local"


def test_a_rejection_names_the_reviewer_who_stopped_the_run():
    """The gate's other decision (#91), attributed from the same durable trail the
    approval is — so a checkpoint whose event text was reworded still says who."""
    rejection = _event(
        "human",
        "rejected",
        "Screening rejected by gate@test.local (lead) — wrong document",
        "2026-07-30T14:02:00+00:00",
    )
    entries = _entries(
        events=[*FULL_LOG[:5], rejection],
        approved_by=None,
        approved_by_role=None,
        approved_at=None,
        rejected_by="gate@test.local",
        rejected_by_role="lead",
        rejected_at="2026-07-30T14:02:00+00:00",
        rejected_reason="wrong document",
        current_step="rejected",
    )

    step = entries[-1]
    assert (step["actor"], step["actor_role"]) == ("gate@test.local", "lead")
    assert step["outcome"] == "Rejected"


def test_a_critic_rejection_is_never_attributed_to_a_person():
    """`rejected` is the Critic's push-back as well as a reviewer's stop, so the
    correlation keys on the agent — a reviewer's name on a machine's verdict would
    misread the entire audit trail."""
    critic_step = next(
        entry for entry in _entries(rejected_by="gate@test.local") if entry["agent"] == "critic"
    )

    assert critic_step["status"] == "rejected"
    assert critic_step["actor"] == ""


def test_the_summary_carries_the_rejection_beside_the_approval():
    summary = _summary(
        rejected_by="gate@test.local",
        rejected_by_role="lead",
        rejected_at="2026-07-30T14:02:00+00:00",
        rejected_reason="Not screenable with this cohort.",
    )

    assert summary["rejected_by"] == "gate@test.local"
    assert summary["rejected_by_role"] == "lead"
    assert summary["rejected_at"] == "2026-07-30T14:02:00+00:00"
    assert summary["rejected_reason"] == "Not screenable with this cohort."


def test_a_run_that_was_never_rejected_reports_empty_rejection_fields():
    """Guarded like every other field here: a checkpoint written before #91 has no
    `rejected_*` keys at all, and reading one must not raise on a finished run."""
    summary = _summary()

    assert summary["rejected_by"] == ""
    assert summary["rejected_reason"] == ""


def test_a_reviewer_edit_pairs_with_the_revision_it_produced():
    edit = next(entry for entry in _entries() if entry["status"] == "edited")

    assert (edit["revision"], edit["actor"], edit["actor_role"]) == (
        1,
        "editor@test.local",
        "reviewer",
    )


def test_a_second_edit_pairs_with_the_second_revision():
    """`events` and `criteria_edits` are both appended in the same update, so the
    Nth edit event is the Nth revision record."""
    second = {
        "revision": 2,
        "edited_by": "other@test.local",
        "edited_by_role": "lead",
        "edited_at": "2026-07-30T14:01:00+00:00",
        "changes": [],
    }
    edits = _values()["criteria_edits"] + [second]
    events = [*FULL_LOG, _event("human", "edited", "revised again", "2026-07-30T14:03:00+00:00")]

    revisions = [
        (entry["revision"], entry["actor"])
        for entry in _entries(events=events, criteria_edits=edits, criteria_revision=2)
        if entry["status"] == "edited"
    ]

    assert revisions == [(1, "editor@test.local"), (2, "other@test.local")]


def test_an_edit_event_with_no_revision_record_still_renders():
    """An older checkpoint can hold the event without the diff record. The step
    keeps its place in the trail; its `detail` still names who acted."""
    edit = next(
        entry
        for entry in _entries(criteria_edits=[], criteria_revision=0)
        if entry["status"] == "edited"
    )

    assert (edit["revision"], edit["actor"]) == (0, "")
    assert "editor@test.local" in edit["detail"]


def test_machine_steps_have_no_actor():
    assert all(entry["actor"] == "" for entry in _entries() if entry["agent"] != "human")


def test_the_summary_reports_the_approval_and_the_revision_count():
    summary = _summary()

    assert summary["approved_by"] == "boss@test.local"
    assert summary["approved_by_role"] == "lead"
    assert summary["approved_at"] == "2026-07-30T14:02:00+00:00"
    assert summary["revisions"] == 1


def test_a_run_nobody_approved_reports_no_approver():
    summary = _summary(approved_by=None, approved_by_role=None, approved_at=None)

    assert (summary["approved_by"], summary["approved_by_role"]) == ("", "")


# --- Timing -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("milliseconds", "expected"),
    [
        (0, "0ms"),
        (999, "999ms"),
        (1000, "1.0s"),
        (59_949, "59.9s"),
        # Rounds up past the minute rather than reading "60.0s".
        (59_950, "1m 0s"),
        (3_599_000, "59m 59s"),
        (3_600_000, "1h 0m"),
        (10_883_400, "3h 1m"),
        # Clock skew is noise, not a negative duration.
        (-5000, "0ms"),
    ],
)
def test_a_span_reads_in_the_coarsest_unit_that_still_says_something(milliseconds, expected):
    assert timeline.format_span(milliseconds) == expected


def test_each_step_carries_the_gap_since_the_one_before_it():
    elapsed = [entry["elapsed"] for entry in _entries()]

    # Nothing to be relative to on the first step; the long wait at the approval
    # gate reads in minutes rather than in milliseconds.
    assert elapsed[0] == ""
    assert elapsed[1] == "+2.0s"
    assert elapsed[2] == "+1.5s"
    # 14:00:01 → 14:02:00 at the gate.
    assert elapsed[7] == "+1m 59s"


def test_a_malformed_timestamp_costs_only_its_own_gap():
    """Regression guard: measuring from the last *readable* stamp keeps one bad
    value from blanking the rest of the trail."""
    events = [
        _event("router", "completed", "admitted", "2026-07-30T13:59:00+00:00"),
        _event("parser", "completed", "extracted", "not-a-timestamp"),
        _event("critic", "completed", "reviewed", "2026-07-30T13:59:04+00:00"),
    ]

    assert [entry["elapsed"] for entry in _entries(events=events)] == ["", "", "+4.0s"]


def test_the_duration_spans_the_whole_run():
    summary = _summary()

    assert summary["started_at"] == "2026-07-30T13:59:00+00:00"
    assert summary["ended_at"] == "2026-07-30T14:02:04+00:00"
    assert summary["duration"] == "3m 4s"


def test_one_skewed_stamp_cannot_make_a_run_look_negative():
    events = [
        _event("router", "completed", "admitted", "2026-07-30T13:59:10+00:00"),
        _event("parser", "completed", "extracted", "2026-07-30T13:59:00+00:00"),
    ]

    assert _summary(events=events)["duration"] == "10.0s"


def test_a_single_step_run_has_no_duration_to_report():
    summary = _summary(events=[FULL_LOG[0]])

    assert summary["duration"] == ""
    assert summary["started_at"] == summary["ended_at"] == FULL_LOG[0]["timestamp"]


def test_a_naive_timestamp_is_read_as_utc_rather_than_crashing_the_subtraction():
    events = [
        _event("router", "completed", "admitted", "2026-07-30T13:59:00"),
        _event("parser", "completed", "extracted", "2026-07-30T13:59:02+00:00"),
    ]

    assert _entries(events=events)[1]["elapsed"] == "+2.0s"


# --- Degrading on a checkpoint this build did not write ----------------------


def test_a_run_with_no_checkpoint_has_an_empty_timeline():
    trail = timeline.build_timeline({})

    assert trail["entries"] == []
    assert trail["summary"]["duration"] == ""
    assert trail["summary"]["attempts"] == 0


@pytest.mark.parametrize("events", [None, "not-a-list", 7, {}])
def test_an_events_field_that_is_not_a_list_yields_no_entries(events):
    assert timeline.build_timeline({"events": events})["entries"] == []


def test_an_entry_missing_every_field_still_renders_as_a_row():
    """The alternative is a 500 on opening a run — the log is an audit record, so
    an unreadable step has to be visible as one."""
    entries = timeline.build_timeline({"events": [{}, "junk", None]})["entries"]

    assert len(entries) == 3
    assert entries[0]["label"] == entries[0]["detail"] == ""
    assert entries[0]["elapsed"] == ""


@pytest.mark.parametrize("revision", [None, "two", {}])
def test_an_unreadable_revision_number_degrades_to_zero(revision):
    assert timeline.build_timeline({"criteria_revision": revision})["summary"]["revisions"] == 0


def test_a_revision_record_that_is_not_a_mapping_leaves_the_step_unattributed():
    edit = timeline.build_timeline(
        {
            "events": [_event("human", "edited", "revised", "2026-07-30T14:00:00+00:00")],
            "criteria_edits": ["junk"],
        }
    )["entries"][0]

    assert (edit["revision"], edit["actor"]) == (0, "")


# --- Route ------------------------------------------------------------------


class FakeSnapshot:
    def __init__(self, values: dict, pending: tuple = ()):
        self.values = values
        self.next = pending


class FakeGraph:
    """Returns one fixed snapshot — reading a timeline never runs the pipeline."""

    def __init__(self, snapshot: FakeSnapshot):
        self.snapshot = snapshot

    async def aget_state(self, _config: object) -> FakeSnapshot:
        return self.snapshot

    async def astream(  # pragma: no cover - the timeline never drives the graph
        self, input: object, config: object = None, *, stream_mode: object = None
    ) -> AsyncIterator[dict]:
        raise NotImplementedError
        yield {}

    async def ainvoke(self, *_a: object, **_k: object) -> dict:  # pragma: no cover
        raise NotImplementedError

    async def aupdate_state(  # pragma: no cover
        self, _config: object, _values: dict, as_node: str | None = None
    ) -> None:
        raise NotImplementedError


@pytest.fixture
def client():
    with TestClient(main.app, raise_server_exceptions=False) as c:
        sign_in(c)
        yield c


def _create(client) -> str:
    upload = client.post(
        "/api/screenings", files={"file": ("protocol.md", PROTOCOL.encode(), "text/markdown")}
    )
    assert upload.status_code == 200
    return str(upload.json()["thread_id"])


def test_the_state_endpoint_serves_the_timeline_beside_the_checkpoint(client, monkeypatch):
    thread_id = _create(client)
    monkeypatch.setattr(main, "graph", FakeGraph(FakeSnapshot(_values())))

    body = client.get(f"/api/screenings/{thread_id}/state").json()

    # Derived, not stored: the raw log is still there untouched, with the trail
    # alongside it.
    assert body["values"]["events"] == FULL_LOG
    assert [entry["label"] for entry in body["timeline"]["entries"][:2]] == ["Router", "Parser"]
    assert body["timeline"]["summary"]["attempts"] == 2
    assert body["timeline"]["summary"]["approved_by"] == "boss@test.local"


def test_a_run_that_never_streamed_serves_an_empty_timeline(client, monkeypatch):
    thread_id = _create(client)
    monkeypatch.setattr(main, "graph", FakeGraph(FakeSnapshot({})))

    body = client.get(f"/api/screenings/{thread_id}/state").json()

    assert body["timeline"] == {
        "entries": [],
        "summary": {
            "started_at": "",
            "ended_at": "",
            "duration": "",
            "attempts": 0,
            "critic_rejections": 0,
            "revisions": 0,
            "escalated": False,
            "approved_by": "",
            "approved_by_role": "",
            "approved_at": "",
            "rejected_by": "",
            "rejected_by_role": "",
            "rejected_at": "",
            "rejected_reason": "",
        },
    }
