"""The downloadable screening report (#56).

Two halves: the renderer as a pure function over a `/state` payload (no app, no
store), and the route that serves it — where the download mechanics and the
"never ran" rejection live.

The escaping tests are the load-bearing ones. Every string in this document comes
from an uploaded protocol by way of an LLM, and the API shares an origin with the
app in the demo topology, so markup that survived into the output would be a
stored-XSS hole and not a typo.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from html import escape
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.services import report, screening
from app.services.criteria_edits import criterion_label
from tests.auth_helpers import ADMIN, REVIEWER, sign_in

GENERATED_AT = datetime(2026, 7, 30, 14, 5, tzinfo=UTC)

# Annotated: these fixtures are heterogeneous checkpoint payloads, and without it
# mypy narrows each to its own union and rejects every index into them.
CRITERIA: dict[str, Any] = {
    "trial_title": "A Phase II Study of Widgetinib in NSCLC",
    "inclusion_quantitative": [
        {
            "attribute": "age",
            "operator": ">=",
            "value": 18.0,
            "value_high": None,
            "unit": "years",
            "source_text": "Age 18 years or older at the time of consent.",
        },
        {
            "attribute": "egfr",
            "operator": "between",
            "value": 30.0,
            "value_high": 60.0,
            "unit": "mL/min/1.73m2",
            "source_text": "eGFR between 30 and 60 mL/min/1.73m2.",
        },
    ],
    "inclusion_categorical": [
        {
            "category": "diagnosis",
            "value": "NSCLC",
            "negated": False,
            "source_text": "Histologically confirmed non-small cell lung cancer.",
        }
    ],
    "exclusion_quantitative": [],
    "exclusion_categorical": [
        {
            "category": "condition",
            "value": "active infection",
            "negated": False,
            "source_text": "Any active systemic infection requiring treatment.",
        }
    ],
    "unparseable": ["Adequate organ function per investigator assessment."],
}

FINDINGS: list[dict[str, Any]] = [
    {
        "rule_id": "HEPATIC-001",
        "severity": "reject",
        "message": "Organ-function criterion carries no numeric threshold.",
        "explanation": "One eligibility rule about organ function has no number attached.",
    },
    {
        "rule_id": "LLM-SEM",
        "severity": "warn",
        "message": "age >= 18 years is unusually permissive for this indication.",
        "explanation": "The lower age limit looks broad for this kind of trial.",
    },
]

COHORT: list[dict[str, Any]] = [
    {
        "patient_id": "PT-1",
        "name": "Ann",
        "eligible": True,
        "needs_review": False,
        "criterion_results": [],
        "summary": "Ann meets every criterion the records could answer.",
    },
    {
        "patient_id": "PT-2",
        "name": "Ben",
        "eligible": False,
        "needs_review": False,
        "criterion_results": [
            {
                "criterion": CRITERIA["inclusion_quantitative"][0],
                "kind": "inclusion",
                "status": "fail",
                "explanation": "Ben is 16, and the trial asks for at least 18.",
            }
        ],
        "summary": "Ben is too young for this trial.",
    },
    {
        # needs_review outranks eligible — the report must bucket this as review.
        "patient_id": "PT-3",
        "name": "Cai",
        "eligible": True,
        "needs_review": True,
        "criterion_results": [
            {
                "criterion": CRITERIA["inclusion_categorical"][0],
                "kind": "inclusion",
                "status": "unknown",
                "explanation": "The records do not say whether Cai has NSCLC.",
            }
        ],
        "summary": "Cai needs a human look: one criterion could not be determined.",
    },
]

EVENTS: list[dict[str, Any]] = [
    {
        "agent": "router",
        "status": "completed",
        "detail": "Input accepted as a protocol.",
        "timestamp": "2026-07-30T13:59:00+00:00",
    },
    {
        "agent": "human",
        "status": "approved",
        "detail": "Approval gate cleared by reviewer@test.local (reviewer)",
        "timestamp": "2026-07-30T14:02:00+00:00",
    },
]


def payload(**overrides: Any) -> dict[str, Any]:
    """A finished run's `/state` payload — the renderer's only input."""
    values: dict[str, Any] = {
        "source_filename": "nsclc_protocol.pdf",
        "current_step": "done",
        "parsed_criteria": CRITERIA,
        "compliance_findings": FINDINGS,
        "compliance_summary": "Two issues found; one must be fixed before screening.",
        "matched_patients": COHORT,
        "match_summary": "1 of 3 patients is eligible; 1 needs review.",
        "approved_by": "reviewer@test.local",
        "approved_by_role": "reviewer",
        "approved_at": "2026-07-30T14:02:00+00:00",
        "criteria_revision": 0,
        "criteria_edits": [],
        "events": EVENTS,
    }
    values.update(overrides.pop("values", {}))
    base: dict[str, Any] = {
        "values": values,
        "pending": [],
        "screening": {
            "thread_id": "7f3c9a10-0000-4000-8000-000000000001",
            "source_filename": "nsclc_protocol.pdf",
            "status": "done",
            "created_at": "2026-07-30T13:58:00+00:00",
            "criteria_count": 4,
            "match_count": 1,
        },
    }
    base.update(overrides)
    return base


def render(**overrides: Any) -> str:
    return report.render_report(payload(**overrides), generated_at=GENERATED_AT)


# --- Document shape ---------------------------------------------------------


def test_report_is_a_complete_branded_dated_document():
    html = render()
    assert html.startswith("<!doctype html>")
    assert html.rstrip().endswith("</html>")
    assert "TrialGate" in html
    assert "nsclc_protocol.pdf" in html
    # Dated, in UTC, and not in the exporting server's locale.
    assert "2026-07-30 14:05 UTC" in html


def test_report_carries_the_synthetic_data_disclaimer():
    html = render()
    assert "Synthetic data — not for clinical use." in html
    # Twice on purpose: at the top for the reader who skims, in the footer for the
    # page that gets printed and detached from the first one.
    assert html.count("Synthetic data — not for clinical use.") == 2


def test_report_references_nothing_outside_itself():
    """Self-contained is the property that makes this artifact archivable."""
    html = render()
    for reference in ("<script", "src=", "<link", "@import", "http://", "https://", "url("):
        assert reference not in html, f"report pulls in {reference!r}"


def test_report_includes_print_styles_so_it_saves_as_a_clean_pdf():
    html = render()
    assert "@media print" in html
    assert "@page" in html
    # A cohort table longer than a page has to repeat its header row.
    assert "table-header-group" in html


# --- Criteria + provenance --------------------------------------------------


def test_every_criterion_appears_beside_its_verbatim_source_sentence():
    html = render()
    for bucket in (
        "inclusion_quantitative",
        "inclusion_categorical",
        "exclusion_categorical",
    ):
        for criterion in CRITERIA[bucket]:
            # Escaped, because that is the only form allowed into the document —
            # `>=` is `&gt;=` on the page and identical in a browser.
            assert escape(criterion_label(criterion)) in html
            assert criterion["source_text"] in html


def test_criteria_labels_match_the_edit_history_rendering():
    """The report and the #53 diff must write a criterion the same way."""
    html = render()
    assert "age &gt;= 18 years" in html
    assert "egfr between 30–60 mL/min/1.73m2" in html
    assert "active infection (condition)" in html


def test_unparseable_sentences_are_reported_not_dropped():
    html = render()
    assert "Not converted to structured criteria" in html
    assert "Adequate organ function per investigator assessment." in html


def test_reviewer_revisions_are_reported_with_who_changed_what():
    html = render(
        values={
            "criteria_revision": 1,
            "criteria_edits": [
                {
                    "revision": 1,
                    "edited_by": "reviewer@test.local",
                    "edited_by_role": "reviewer",
                    "edited_at": "2026-07-30T14:01:00+00:00",
                    "changes": [
                        {
                            "bucket": "inclusion_quantitative",
                            "from_bucket": None,
                            "kind": "modified",
                            "before": "age >= 18 years",
                            "after": "age >= 21 years",
                        }
                    ],
                }
            ],
        }
    )
    assert "Reviewer revisions" in html
    assert "Criteria revision" in html
    assert "age &gt;= 21 years" in html
    assert "reviewer@test.local" in html


def test_an_unrevised_run_has_no_revisions_section():
    html = render()
    assert "Reviewer revisions" not in html
    # Nor a version number nobody asked about.
    assert "Criteria revision" not in html


# --- Findings, authorization, cohort ----------------------------------------


def test_findings_carry_both_the_plain_and_the_technical_layer():
    html = render()
    for finding in FINDINGS:
        assert escape(finding["explanation"]) in html
        assert escape(finding["message"]) in html
        assert finding["rule_id"] in html
    # Severity in the reviewer's terms, not the engine's enum.
    assert "Must fix" in html
    assert "Advisory" in html


def test_a_finding_with_no_plain_layer_falls_back_to_its_technical_wording():
    """A run screened before #52 has no `explanation` — the row must still say
    something."""
    html = render(
        values={
            "compliance_findings": [
                {"rule_id": "PREG-001", "severity": "reject", "message": "No pregnancy criterion."}
            ]
        }
    )
    assert "No pregnancy criterion." in html


def test_the_authorization_line_names_who_cleared_the_gate():
    html = render()
    assert "Patient matching authorized by" in html
    assert "reviewer@test.local" in html
    assert "2026-07-30 14:02 UTC" in html


def test_cohort_lists_every_patient_with_needs_review_outranking_eligible():
    html = render()
    for evaluation in COHORT:
        assert evaluation["patient_id"] in html
        assert evaluation["summary"] in html
    assert "1 eligible" in html
    assert "1 needs review" in html
    assert "1 ineligible" in html


def test_cohort_rows_show_the_criteria_behind_a_non_pass_verdict():
    html = render()
    # The technical layer for PT-2: its status, the criterion, and its provenance.
    assert "fail — age &gt;= 18 years · Age 18 years or older at the time of consent." in html
    assert "unknown — NSCLC (diagnosis) · Histologically confirmed" in html


def test_execution_log_is_reported_in_order():
    html = render()
    assert "Execution log" in html
    for entry in EVENTS:
        assert entry["detail"] in html
    assert html.index(EVENTS[0]["detail"]) < html.index(EVENTS[1]["detail"])


def test_execution_log_names_the_steps_and_the_reviewer_who_cleared_the_gate():
    """The log is rendered from `services.timeline` (#55), so the document carries
    the same resolved labels and identities the run detail view shows."""
    html = render()
    assert "Router" in html
    assert "Reviewer" in html
    assert "Approved" in html
    # Attributed from the durable approval trail, not from the event's sentence.
    assert "reviewer@test.local (reviewer)" in html
    # The gap between two steps, so a reader can see where the run spent its time.
    assert "+3m 0s" in html


def test_execution_log_leads_with_the_shape_of_the_run():
    html = render(
        values={
            "events": [
                *EVENTS,
                {
                    "agent": "parser",
                    "status": "completed",
                    "detail": "Extracted (attempt 1)",
                    "timestamp": "2026-07-30T13:59:10+00:00",
                },
                {
                    "agent": "critic",
                    "status": "rejected",
                    "detail": "2 findings (1 blocking)",
                    "timestamp": "2026-07-30T13:59:12+00:00",
                },
                {
                    "agent": "parser",
                    "status": "completed",
                    "detail": "Extracted (attempt 2)",
                    "timestamp": "2026-07-30T13:59:20+00:00",
                },
            ]
        }
    )
    assert "2 extraction attempts" in html
    assert "1 Critic rejection" in html
    # Numbered only because this run looped — see `_events_section`.
    assert "attempt 2" in html


def test_a_run_that_never_looped_does_not_number_its_one_attempt():
    html = render(
        values={
            "events": [
                *EVENTS,
                {
                    "agent": "parser",
                    "status": "completed",
                    "detail": "Extracted (attempt 1)",
                    "timestamp": "2026-07-30T13:59:10+00:00",
                },
            ]
        }
    )
    assert "attempt 1</div>" not in html
    assert "extraction attempts" not in html


# --- Partial runs -----------------------------------------------------------


def test_a_run_parked_at_the_gate_says_why_there_is_no_cohort():
    html = report.render_report(
        payload(
            pending=["matcher"],
            values={"matched_patients": [], "match_summary": "", "approved_by": None},
        ),
        generated_at=GENERATED_AT,
    )
    assert "parked at the approval gate" in html
    assert "Awaiting approval" in html
    # No empty tables where the run simply hasn't got there yet.
    assert "Cohort" not in html
    assert "Authorization" not in html


def test_an_escalated_run_reports_its_findings_and_explains_the_missing_cohort():
    html = report.render_report(
        payload(
            screening={
                "thread_id": "abc",
                "source_filename": "p.pdf",
                "status": "escalated",
                "created_at": "2026-07-30T13:58:00+00:00",
                "criteria_count": 4,
                "match_count": 0,
            },
            values={"matched_patients": [], "approved_by": None, "current_step": "escalated"},
        ),
        generated_at=GENERATED_AT,
    )
    assert "escalated for human review" in html
    assert "Compliance findings" in html


def test_a_checkpointless_payload_renders_rather_than_raising():
    """`has_reportable_content` gates the route, but the renderer must not be the
    thing that decides — a half-written checkpoint is a document with fewer
    sections, never a 500 on a download."""
    html = report.render_report(
        {"values": {}, "pending": [], "screening": None}, generated_at=GENERATED_AT
    )
    assert "TrialGate" in html
    assert "Cohort" not in html


@pytest.mark.parametrize(
    "values",
    [
        {"compliance_findings": "not a list"},
        {"matched_patients": {"nope": 1}},
        {"events": None},
        {"parsed_criteria": {"inclusion_quantitative": "not a list"}},
        {"matched_patients": ["not a mapping"]},
    ],
)
def test_malformed_checkpoint_fields_degrade_to_empty_sections(values):
    html = render(values=values)
    assert html.startswith("<!doctype html>")


# --- Escaping ---------------------------------------------------------------

INJECTION = '<script>alert("xss")</script>'


@pytest.mark.parametrize(
    "values",
    [
        {"parsed_criteria": {**CRITERIA, "trial_title": INJECTION}},
        {
            "parsed_criteria": {
                **CRITERIA,
                "inclusion_quantitative": [
                    {**CRITERIA["inclusion_quantitative"][0], "source_text": INJECTION}
                ],
            }
        },
        {
            "parsed_criteria": {
                **CRITERIA,
                "inclusion_categorical": [
                    {**CRITERIA["inclusion_categorical"][0], "value": INJECTION}
                ],
            }
        },
        {"parsed_criteria": {**CRITERIA, "unparseable": [INJECTION]}},
        {"compliance_findings": [{"rule_id": INJECTION, "severity": "warn", "message": INJECTION}]},
        {"compliance_summary": INJECTION},
        {"match_summary": INJECTION},
        {"matched_patients": [{**COHORT[0], "name": INJECTION, "summary": INJECTION}]},
        {"approved_by": INJECTION},
        {"events": [{**EVENTS[0], "detail": INJECTION}]},
        {
            "criteria_revision": 1,
            "criteria_edits": [
                {
                    "revision": 1,
                    "edited_by": INJECTION,
                    "edited_by_role": "reviewer",
                    "edited_at": "2026-07-30T14:01:00+00:00",
                    "changes": [
                        {
                            "bucket": "inclusion_quantitative",
                            "from_bucket": None,
                            "kind": "modified",
                            "before": INJECTION,
                            "after": INJECTION,
                        }
                    ],
                }
            ],
        },
    ],
)
def test_markup_from_the_protocol_never_survives_into_the_document(values):
    html = render(values=values)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_a_malicious_filename_is_escaped_in_the_masthead_and_the_title():
    """The filename reaches two places the criteria don't — `<title>` and the
    masthead — so it gets its own case."""
    html = report.render_report(
        payload(
            screening={
                "thread_id": "abc",
                "source_filename": f"{INJECTION}.pdf",
                "status": "done",
                "created_at": "2026-07-30T13:58:00+00:00",
                "criteria_count": 1,
                "match_count": 0,
            }
        ),
        generated_at=GENERATED_AT,
    )
    assert "<script>" not in html
    assert html.count("&lt;script&gt;") == 2


# --- Filename + reportability ----------------------------------------------


def test_report_filename_names_the_protocol_and_the_run():
    assert report.report_filename(payload()) == "trialgate-report-nsclc_protocol-7f3c9a10.html"


@pytest.mark.parametrize(
    "filename",
    ['evil"; rm -rf /.pdf', "line\r\nbreak.pdf", "../../etc/passwd", "", "🙂.pdf"],
)
def test_report_filename_is_header_safe_whatever_the_upload_was_called(filename):
    name = report.report_filename(
        payload(
            screening={
                "thread_id": "abc-def",
                "source_filename": filename,
                "status": "done",
                "created_at": "2026-07-30T13:58:00+00:00",
                "criteria_count": 0,
                "match_count": 0,
            }
        )
    )
    assert name.endswith(".html")
    assert set(name) <= set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")


@pytest.mark.parametrize(
    ("values", "reportable"),
    [
        ({}, False),
        ({"events": []}, False),
        ({"source_filename": "p.pdf"}, False),
        ({"events": [EVENTS[0]]}, True),
        ({"parsed_criteria": CRITERIA}, True),
    ],
)
def test_only_a_run_that_actually_ran_is_reportable(values, reportable):
    assert report.has_reportable_content({"values": values}) is reportable


# --- Route ------------------------------------------------------------------


class FakeSnapshot:
    def __init__(self, values: dict, pending: tuple = ()):
        self.values = values
        self.next = pending


class FakeGraph:
    """Returns one fixed snapshot — the report path only ever reads state.

    The other three `ScreeningGraph` methods are stubbed to satisfy the protocol;
    a report that reached any of them would be executing the pipeline, which is
    exactly what exporting a past run must never do.
    """

    def __init__(self, snapshot: FakeSnapshot):
        self.snapshot = snapshot

    async def aget_state(self, _config: object) -> FakeSnapshot:
        return self.snapshot

    async def astream(  # pragma: no cover - a report never drives the graph
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
        "/api/screenings", files={"file": ("nsclc_protocol.md", b"Inclusion criteria: age >= 18")}
    )
    assert upload.status_code == 200
    return str(upload.json()["thread_id"])


def test_download_serves_the_report_as_an_attachment(client, monkeypatch):
    thread_id = _create(client)
    monkeypatch.setattr(main, "graph", FakeGraph(FakeSnapshot(payload()["values"])))

    response = client.get(f"/api/screenings/{thread_id}/report")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment; filename=")
    assert disposition.endswith('.html"')
    # Pinned down as a file, not a page in our own origin.
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "default-src 'none'" in response.headers["content-security-policy"]
    assert "TrialGate" in response.text
    assert "Age 18 years or older at the time of consent." in response.text


def test_download_names_the_file_after_the_uploaded_protocol(client, monkeypatch):
    thread_id = _create(client)
    monkeypatch.setattr(main, "graph", FakeGraph(FakeSnapshot(payload()["values"])))

    response = client.get(f"/api/screenings/{thread_id}/report")

    assert "nsclc_protocol" in response.headers["content-disposition"]
    assert thread_id[:8] in response.headers["content-disposition"]


def test_report_reflects_the_stored_phase_not_the_checkpoints(client, monkeypatch):
    """The route feeds the renderer `get_screening_state`'s payload, so the store
    row is what names the phase — the same precedence the detail view uses."""
    thread_id = _create(client)
    monkeypatch.setattr(
        main, "graph", FakeGraph(FakeSnapshot(payload()["values"], pending=("matcher",)))
    )

    response = client.get(f"/api/screenings/{thread_id}/report")

    assert "Awaiting approval" in response.text


def test_download_for_a_run_that_never_ran_is_a_409(client, monkeypatch):
    thread_id = _create(client)
    monkeypatch.setattr(main, "graph", FakeGraph(FakeSnapshot({})))

    response = client.get(f"/api/screenings/{thread_id}/report")

    assert response.status_code == 409
    assert response.json()["error"] == "ScreeningNotReportableError"


def test_download_for_an_unknown_thread_is_a_404(client):
    response = client.get("/api/screenings/unknown-id/report")
    assert response.status_code == 404
    assert response.json()["error"] == "ScreeningNotFoundError"


def test_download_requires_a_session():
    with TestClient(main.app, raise_server_exceptions=False) as anonymous:
        response = anonymous.get("/api/screenings/any-id/report")
    assert response.status_code == 401
    # Not a 404: an anonymous caller must not learn whether a run exists.
    assert response.json()["error"] == "AuthenticationRequiredError"


def test_an_admin_can_download_a_report_too(monkeypatch):
    with TestClient(main.app, raise_server_exceptions=False) as c:
        sign_in(c, ADMIN)
        thread_id = _create(c)
        monkeypatch.setattr(main, "graph", FakeGraph(FakeSnapshot(payload()["values"])))
        assert c.get(f"/api/screenings/{thread_id}/report").status_code == 200


async def test_service_returns_the_document_and_its_filename():
    """The service layer is drivable without HTTP, like every other use-case."""
    from app.persistence import InMemoryScreeningStore

    store = InMemoryScreeningStore()
    thread_id = await screening.create_screening(store, "nsclc_protocol.txt", b"text")
    graph = FakeGraph(FakeSnapshot(payload()["values"]))

    filename, document = await screening.get_screening_report(store, graph, thread_id, REVIEWER)

    assert filename.startswith("trialgate-report-nsclc_protocol-")
    assert document.startswith("<!doctype html>")


async def test_exporting_a_report_logs_who_took_the_copy(capsys):
    """The document carries patient data out of the app, so the export is
    attributed in the log — and only there: replaying a run must not write to it.

    Format-agnostic, like the other log assertions: the event name and the email
    appear under both renderers.
    """
    from app.persistence import InMemoryScreeningStore

    store = InMemoryScreeningStore()
    thread_id = await screening.create_screening(store, "p.txt", b"text")
    graph = FakeGraph(FakeSnapshot(payload()["values"]))

    await screening.get_screening_report(store, graph, thread_id, REVIEWER)

    exported = [
        line for line in capsys.readouterr().out.splitlines() if "screening.report_exported" in line
    ]
    assert exported, "expected a screening.report_exported log line"
    assert REVIEWER.email in exported[0]
