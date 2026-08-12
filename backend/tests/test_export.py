"""The machine-readable cohort export (#102).

Two halves, like the report suite: the renderers as pure functions over a `/state`
payload, and the route that serves them.

The load-bearing tests here are the *agreement* ones — that a bucket in the CSV is
the bucket `services/cohort.py` assigns and the report prints — and the
spreadsheet ones. A CSV is a file another program executes: Excel and Sheets treat
a leading `=` as a formula, and every label in this export came out of an uploaded
document by way of an LLM. That is the same class of hole `test_report`'s escaping
tests cover, in a different renderer.
"""

import csv
import io
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.services import cohort, export, report, screening
from app.services.criteria_edits import criterion_label
from tests.auth_helpers import REVIEWER, sign_in

GENERATED_AT = datetime(2026, 8, 10, 9, 30, tzinfo=UTC)

AGE = {
    "attribute": "age",
    "operator": ">=",
    "value": 18.0,
    "value_high": None,
    "unit": "years",
    "source_text": "Age 18 years or older at the time of consent.",
}
EGFR = {
    "attribute": "egfr",
    "operator": "between",
    "value": 30.0,
    "value_high": 60.0,
    "unit": "mL/min/1.73m2",
    "source_text": "eGFR between 30 and 60 mL/min/1.73m2.",
}
NSCLC = {
    "category": "diagnosis",
    "value": "NSCLC",
    "negated": False,
    "source_text": "Histologically confirmed non-small cell lung cancer.",
}
INFECTION = {
    "category": "condition",
    "value": "active infection",
    "negated": False,
    "source_text": "Any active systemic infection requiring treatment.",
}

CRITERIA: dict[str, Any] = {
    "trial_title": "A Phase II Study of Widgetinib in NSCLC",
    "inclusion_quantitative": [AGE, EGFR],
    "inclusion_categorical": [NSCLC],
    "exclusion_quantitative": [],
    "exclusion_categorical": [INFECTION],
    "unparseable": ["Adequate organ function per investigator assessment."],
}

COHORT: list[dict[str, Any]] = [
    {
        "patient_id": "PT-1",
        "name": "Ann",
        "eligible": True,
        "needs_review": False,
        "criterion_results": [
            {"criterion": AGE, "kind": "inclusion", "status": "pass", "explanation": "Ann is 54."},
            {
                "criterion": EGFR,
                "kind": "inclusion",
                "status": "pass",
                "explanation": "Ann's eGFR is 44 mL/min/1.73m2.",
            },
            {
                "criterion": NSCLC,
                "kind": "inclusion",
                "status": "pass",
                "explanation": "Ann's record lists NSCLC.",
            },
            {
                "criterion": INFECTION,
                "kind": "exclusion",
                "status": "pass",
                "explanation": "No active infection on record.",
            },
        ],
        "summary": "Ann meets every criterion the records could answer.",
    },
    {
        "patient_id": "PT-2",
        "name": "Ben",
        "eligible": False,
        "needs_review": False,
        "criterion_results": [
            {
                "criterion": AGE,
                "kind": "inclusion",
                "status": "fail",
                "explanation": "Ben is 16, and the trial asks for at least 18.",
            }
        ],
        "summary": "Ben is too young for this trial.",
    },
    {
        # needs_review outranks eligible — every reader must bucket this as review.
        "patient_id": "PT-3",
        "name": "Cai",
        "eligible": True,
        "needs_review": True,
        "criterion_results": [
            {
                "criterion": NSCLC,
                "kind": "inclusion",
                "status": "unknown",
                "explanation": "The records do not say whether Cai has NSCLC.",
            }
        ],
        "summary": "Cai needs a human look: one criterion could not be determined.",
    },
]


def payload(**overrides: Any) -> dict[str, Any]:
    """A finished run's `/state` payload — the renderers' only input."""
    values: dict[str, Any] = {
        "source_filename": "nsclc_protocol.pdf",
        "current_step": "done",
        "parsed_criteria": CRITERIA,
        "matched_patients": COHORT,
        "match_summary": "1 of 3 patients is eligible; 1 needs review.",
        "approved_by": "reviewer@test.local",
        "approved_by_role": "reviewer",
        "approved_at": "2026-08-10T09:02:00+00:00",
        "criteria_revision": 0,
        "events": [{"agent": "router", "status": "completed", "detail": "Accepted."}],
    }
    values.update(overrides.pop("values", {}))
    base: dict[str, Any] = {
        "values": values,
        "pending": [],
        "screening": {
            "thread_id": "7f3c9a10-0000-4000-8000-000000000001",
            "source_filename": "nsclc_protocol.pdf",
            "status": "done",
            "created_at": "2026-08-10T09:00:00+00:00",
            "criteria_count": 4,
            "match_count": 1,
        },
    }
    base.update(overrides)
    return base


def read_csv(**overrides: Any) -> tuple[list[str], list[list[str]]]:
    """The rendered CSV parsed back, as `(header, rows)` — BOM stripped."""
    text = export.render_csv(payload(**overrides))
    assert text.startswith("\ufeff")
    parsed = list(csv.reader(io.StringIO(text[1:], newline="")))
    return parsed[0], parsed[1:]


def build(**overrides: Any) -> dict[str, Any]:
    return export.build_export(payload(**overrides), generated_at=GENERATED_AT)


# --- CSV shape --------------------------------------------------------------


def test_csv_is_one_row_per_patient_with_the_fixed_columns_first():
    header, rows = read_csv()
    assert header[:5] == ["patient_id", "patient_name", "bucket", "bucket_label", "assessment"]
    assert [row[0] for row in rows] == ["PT-1", "PT-2", "PT-3"]


def test_csv_carries_a_column_per_extracted_criterion_in_reading_order():
    """Inclusion before exclusion, numeric before categorical — the report's order,
    so a reader holding both artifacts walks the criteria in one sequence."""
    header, _rows = read_csv()
    assert header[5:] == [
        criterion_label(AGE),
        criterion_label(EGFR),
        criterion_label(NSCLC),
        criterion_label(INFECTION),
    ]


def test_csv_states_each_patients_verdict_under_each_criterion():
    header, rows = read_csv()
    age = header.index(criterion_label(AGE))
    ben = next(row for row in rows if row[0] == "PT-2")
    assert ben[age] == "fail"


def test_a_criterion_the_patient_was_never_scored_on_says_so():
    """Blank would read as a silent pass in a column of `pass` (#93's distinction)."""
    header, rows = read_csv()
    egfr = header.index(criterion_label(EGFR))
    ben = next(row for row in rows if row[0] == "PT-2")
    assert ben[egfr] == "not evaluated"


def test_csv_starts_with_a_utf8_bom_and_uses_crlf_rows():
    """Both are what make the file open cleanly in Excel rather than merely open."""
    text = export.render_csv(payload())
    assert text.startswith("\ufeff")
    assert "\r\n" in text
    assert text.encode("utf-8").startswith(b"\xef\xbb\xbf")


def test_free_text_with_commas_quotes_and_newlines_stays_in_one_cell():
    """AC 5's "quoted free text": a protocol sentence with a comma in it must not
    split a row, and one with a quote must not terminate a field early."""
    hostile = 'Ineligible: age, "weight", and\nrenal function.'
    header, rows = read_csv(
        values={
            "matched_patients": [
                {
                    "patient_id": "PT-9",
                    "eligible": False,
                    "needs_review": False,
                    "criterion_results": [],
                    "summary": hostile,
                }
            ]
        }
    )
    assert len(rows) == 1
    assert rows[0][header.index("assessment")] == hostile


def test_a_run_with_no_cohort_exports_a_header_and_no_rows():
    """A parked run still says what it screens on; it just has nobody under it."""
    header, rows = read_csv(values={"matched_patients": []})
    assert rows == []
    assert criterion_label(AGE) in header


# --- Spreadsheet safety -----------------------------------------------------


def test_a_cell_that_would_execute_as_a_formula_is_defused():
    """Every string here came from an uploaded document by way of an LLM, and a
    cell opening with `=` is a formula in both Excel and Sheets."""
    _header, rows = read_csv(
        values={
            "matched_patients": [
                {
                    "patient_id": "=cmd|'/c calc'!A1",
                    "name": "+SUM(A1)",
                    "eligible": True,
                    "needs_review": False,
                    "criterion_results": [],
                    "summary": "@SUM(1:2)",
                }
            ]
        }
    )
    assert rows[0][0].startswith("'=")
    assert rows[0][1].startswith("'+")
    assert rows[0][4].startswith("'@")


def test_a_criterion_label_that_would_execute_as_a_formula_is_defused():
    """The header row is untrusted too — the labels are built from LLM output."""
    hostile = {**NSCLC, "value": '=HYPERLINK("http://evil")'}
    header, _rows = read_csv(
        values={"parsed_criteria": {**CRITERIA, "inclusion_categorical": [hostile]}}
    )
    assert any(cell.startswith("'") for cell in header[5:])


def test_a_negative_number_is_left_as_a_number():
    """`-` leads the formula list, so exempting numerics is what keeps a real lab
    value from arriving in the spreadsheet as text."""
    assert export.csv_cell("-3.2") == "-3.2"
    assert export.csv_cell("-") == "'-"
    assert export.csv_cell("-cmd") == "'-cmd"


# --- Buckets agree with every other reader ----------------------------------


def test_the_bucket_column_is_services_cohorts_own_verdict():
    _header, rows = read_csv()
    for row, evaluation in zip(rows, COHORT, strict=True):
        assert row[2] == cohort.bucket_of(evaluation)
        assert row[3] == cohort.BUCKET_LABELS[cohort.bucket_of(evaluation)]


def test_needs_review_outranks_eligible_here_as_everywhere():
    _header, rows = read_csv()
    cai = next(row for row in rows if row[0] == "PT-3")
    assert cai[2] == "review"


def test_the_export_and_the_report_agree_on_who_was_eligible():
    """The acceptance criterion, asserted against the other artifact rather than
    against a literal: two renderings of one snapshot cannot disagree."""
    document = report.render_report(payload(), generated_at=GENERATED_AT)
    counts = build()["counts"]
    for bucket, count in counts.items():
        assert f"{count} {cohort.BUCKET_LABELS[bucket].lower()}" in document


def test_json_counts_are_bucket_counts_over_the_same_cohort():
    assert build()["counts"] == cohort.bucket_counts(COHORT)


# --- JSON is self-describing ------------------------------------------------


def test_json_carries_the_approved_criteria_with_their_source_text():
    """AC 2: an export has to be auditable without the app that produced it."""
    criteria = build()["criteria"]
    assert [entry["source_text"] for entry in criteria] == [
        AGE["source_text"],
        EGFR["source_text"],
        NSCLC["source_text"],
        INFECTION["source_text"],
    ]
    assert {entry["kind"] for entry in criteria} == {"inclusion", "exclusion"}


def test_json_names_the_run_the_trial_and_when_it_was_exported():
    run = build()["run"]
    assert run["thread_id"] == "7f3c9a10-0000-4000-8000-000000000001"
    assert run["source_filename"] == "nsclc_protocol.pdf"
    assert run["trial_title"] == CRITERIA["trial_title"]
    assert run["exported_at"] == "2026-08-10T09:30:00+00:00"


def test_json_records_who_authorized_the_matching():
    assert build()["authorization"]["approved_by"] == "reviewer@test.local"


def test_json_states_an_absent_approval_rather_than_omitting_the_key():
    authorization = build(values={"approved_by": None, "approved_by_role": None})["authorization"]
    assert authorization["approved_by"] is None


def test_json_carries_the_sentences_that_never_became_criteria():
    """The reviewer's check-by-hand list. An export that dropped them would present
    a partial screen as a whole one."""
    assert build()["unparseable"] == CRITERIA["unparseable"]


def test_json_carries_the_disclaimer_the_report_carries():
    assert build()["disclaimer"] == report.DISCLAIMER


def test_every_patient_carries_a_result_for_every_criterion():
    """Same length and same index for every patient, so a consumer can zip them."""
    document = build()
    keys = [entry["key"] for entry in document["criteria"]]
    for patient in document["patients"]:
        assert [result["criterion_key"] for result in patient["results"]] == keys


def test_a_json_result_restates_the_criterion_and_the_matchers_sentence():
    ben = next(p for p in build()["patients"] if p["patient_id"] == "PT-2")
    age = next(r for r in ben["results"] if r["label"] == criterion_label(AGE))
    assert age["status"] == "fail"
    assert age["source_text"] == AGE["source_text"]
    assert "16" in age["explanation"]


def test_the_export_is_json_serializable():
    """It is served as a body, so a value the encoder chokes on is a 500 on a
    download rather than a test failure somewhere quieter."""
    assert json.loads(json.dumps(build()))["patients"][0]["patient_id"] == "PT-1"


# --- Defensive reads --------------------------------------------------------


def test_a_criterion_scored_but_missing_from_the_extraction_still_gets_a_column():
    """An older checkpoint's cohort must not lose verdicts silently."""
    orphan = {
        "category": "condition",
        "value": "prior therapy",
        "negated": False,
        "source_text": "No prior systemic therapy.",
    }
    header, rows = read_csv(
        values={
            "matched_patients": [
                {
                    "patient_id": "PT-9",
                    "eligible": False,
                    "needs_review": False,
                    "criterion_results": [
                        {"criterion": orphan, "kind": "exclusion", "status": "fail"}
                    ],
                }
            ]
        }
    )
    # Appended after the extraction's own four, so the stable prefix stays stable.
    assert header[5:9] == [
        criterion_label(AGE),
        criterion_label(EGFR),
        criterion_label(NSCLC),
        criterion_label(INFECTION),
    ]
    assert header[9] == criterion_label(orphan)
    assert rows[0][9] == "fail"


def test_the_worst_verdict_wins_when_one_criterion_was_applied_twice():
    """A criterion a patient both passed and failed must not filter into the
    enrollable column."""
    _header, rows = read_csv(
        values={
            "parsed_criteria": {**CRITERIA, "inclusion_quantitative": [AGE]},
            "matched_patients": [
                {
                    "patient_id": "PT-9",
                    "eligible": True,
                    "needs_review": False,
                    "criterion_results": [
                        {
                            "criterion": AGE,
                            "kind": "inclusion",
                            "status": "pass",
                            "explanation": "ok",
                        },
                        {
                            "criterion": AGE,
                            "kind": "inclusion",
                            "status": "fail",
                            "explanation": "no",
                        },
                    ],
                }
            ],
        }
    )
    assert rows[0][5] == "fail"


def test_the_explanation_describes_the_status_printed_beside_it():
    document = build(
        values={
            "parsed_criteria": {**CRITERIA, "inclusion_quantitative": [AGE]},
            "matched_patients": [
                {
                    "patient_id": "PT-9",
                    "eligible": True,
                    "needs_review": False,
                    "criterion_results": [
                        {
                            "criterion": AGE,
                            "kind": "inclusion",
                            "status": "pass",
                            "explanation": "ok",
                        },
                        {
                            "criterion": AGE,
                            "kind": "inclusion",
                            "status": "fail",
                            "explanation": "no",
                        },
                    ],
                }
            ],
        }
    )
    age = document["patients"][0]["results"][0]
    assert (age["status"], age["explanation"]) == ("fail", "no")


def test_a_recorded_fail_survives_a_malformed_row_beside_it():
    """A `fail` is a verdict the Matcher actually recorded. An unreadable row next
    to it must not mask it — the cell a coordinator filters on has to show the
    failure, not a blank."""
    _header, rows = read_csv(
        values={
            "parsed_criteria": {**CRITERIA, "inclusion_quantitative": [AGE]},
            "matched_patients": [
                {
                    "patient_id": "PT-9",
                    "eligible": False,
                    "needs_review": False,
                    "criterion_results": [
                        {"criterion": AGE, "kind": "inclusion", "status": "fail"},
                        # No status at all — an older build's row, or a hand edit.
                        {"criterion": AGE, "kind": "inclusion"},
                    ],
                }
            ],
        }
    )
    assert rows[0][5] == "fail"


def test_a_result_row_with_no_status_says_so_rather_than_rendering_blank():
    _header, rows = read_csv(
        values={
            "parsed_criteria": {**CRITERIA, "inclusion_quantitative": [AGE]},
            "matched_patients": [
                {
                    "patient_id": "PT-9",
                    "eligible": True,
                    "needs_review": False,
                    "criterion_results": [{"criterion": AGE, "kind": "inclusion"}],
                }
            ],
        }
    )
    assert rows[0][5] == "not evaluated"


def test_a_result_row_with_no_kind_folds_into_the_criterions_own_column():
    """Keying it directly would mint a second column under a byte-identical header,
    with the verdict in one and "not evaluated" in the other."""
    header, rows = read_csv(
        values={
            "parsed_criteria": {**CRITERIA, "inclusion_quantitative": [AGE]},
            "matched_patients": [
                {
                    "patient_id": "PT-9",
                    "eligible": False,
                    "needs_review": False,
                    "criterion_results": [{"criterion": AGE, "status": "fail"}],
                }
            ],
        }
    )
    assert header.count(criterion_label(AGE)) == 1
    assert rows[0][header.index(criterion_label(AGE))] == "fail"


@pytest.mark.parametrize(
    "values",
    [
        {"parsed_criteria": None, "matched_patients": None},
        {"parsed_criteria": "not a mapping", "matched_patients": "not a list"},
        {"matched_patients": [None, {"patient_id": "PT-9", "criterion_results": [{}]}]},
        {"parsed_criteria": {**CRITERIA, "unparseable": "one sentence"}},
    ],
)
def test_a_malformed_checkpoint_degrades_rather_than_raising(values):
    """A checkpoint written by an older build, or hand-edited in a demo, must not
    500 a download — the same guarantee `services/checkpoint.py` exists for."""
    export.render_csv(payload(values=values))
    export.build_export(payload(values=values))


def test_an_unrecognized_status_is_not_reported_as_a_pass():
    _header, rows = read_csv(
        values={
            "parsed_criteria": {**CRITERIA, "inclusion_quantitative": [AGE]},
            "matched_patients": [
                {
                    "patient_id": "PT-9",
                    "eligible": True,
                    "needs_review": False,
                    "criterion_results": [
                        {"criterion": AGE, "kind": "inclusion", "status": "pass"},
                        {"criterion": AGE, "kind": "inclusion", "status": "deferred"},
                    ],
                }
            ],
        }
    )
    assert rows[0][5] == "deferred"


# --- Filenames --------------------------------------------------------------


def test_the_filename_names_the_protocol_the_run_and_the_format():
    assert export.export_filename(payload(), "csv") == (
        "trialgate-cohort-nsclc_protocol-7f3c9a10.csv"
    )
    assert export.export_filename(payload(), "json").endswith(".json")


def test_the_filename_cannot_carry_a_header_injection():
    """It is interpolated into `Content-Disposition`, where a quote or a newline
    would be an injection rather than a cosmetic bug."""
    hostile = payload()
    hostile["screening"]["source_filename"] = 'evil";\r\nX-Injected: yes'
    name = export.export_filename(hostile, "csv")
    assert '"' not in name and "\r" not in name and "\n" not in name


# --- Route ------------------------------------------------------------------


class FakeSnapshot:
    def __init__(self, values: dict, pending: tuple = ()):
        self.values = values
        self.next = pending


class FakeGraph:
    """Returns one fixed snapshot — an export only ever reads state.

    The other three `ScreeningGraph` methods raise: an export that reached any of
    them would be executing the pipeline, which is exactly what downloading a past
    run's cohort must never do.
    """

    def __init__(self, snapshot: FakeSnapshot):
        self.snapshot = snapshot

    async def aget_state(self, _config: object) -> FakeSnapshot:
        return self.snapshot

    async def astream(  # pragma: no cover - an export never drives the graph
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


def test_export_defaults_to_csv_and_is_served_as_an_attachment(client, monkeypatch):
    thread_id = _create(client)
    monkeypatch.setattr(main, "graph", FakeGraph(FakeSnapshot(payload()["values"])))

    response = client.get(f"/api/screenings/{thread_id}/export")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment; filename=")
    assert disposition.endswith('.csv"')
    # Pinned down as a file, not a page in our own origin.
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "default-src 'none'" in response.headers["content-security-policy"]
    assert response.content.startswith(b"\xef\xbb\xbf")
    assert "PT-1" in response.text


def test_export_serves_json_on_request(client, monkeypatch):
    thread_id = _create(client)
    monkeypatch.setattr(main, "graph", FakeGraph(FakeSnapshot(payload()["values"])))

    response = client.get(f"/api/screenings/{thread_id}/export?format=json")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["content-disposition"].endswith('.json"')
    document = response.json()
    assert [p["patient_id"] for p in document["patients"]] == ["PT-1", "PT-2", "PT-3"]
    assert document["criteria"][0]["source_text"] == AGE["source_text"]


def test_an_unknown_format_is_refused_rather_than_silently_served_as_csv(client, monkeypatch):
    thread_id = _create(client)
    monkeypatch.setattr(main, "graph", FakeGraph(FakeSnapshot(payload()["values"])))

    response = client.get(f"/api/screenings/{thread_id}/export?format=xlsx")

    assert response.status_code == 422


def test_a_run_that_never_streamed_is_a_409(client, monkeypatch):
    """The same refusal the report makes, for the same run: the two downloads sit
    beside each other and must not disagree about which runs they serve."""
    thread_id = _create(client)
    monkeypatch.setattr(main, "graph", FakeGraph(FakeSnapshot({})))

    export_response = client.get(f"/api/screenings/{thread_id}/export")
    report_response = client.get(f"/api/screenings/{thread_id}/report")

    assert export_response.status_code == 409
    assert report_response.status_code == 409


def test_a_parsed_run_with_no_cohort_still_exports(client, monkeypatch):
    """A parked run's export is its criteria over an empty cohort — a true
    statement, where a 409 would tell a script the run does not exist."""
    thread_id = _create(client)
    values = payload(values={"matched_patients": []})["values"]
    monkeypatch.setattr(main, "graph", FakeGraph(FakeSnapshot(values, pending=("matcher",))))

    response = client.get(f"/api/screenings/{thread_id}/export?format=json")

    assert response.status_code == 200
    assert response.json()["patients"] == []
    assert response.json()["criteria"]


def test_export_of_an_unknown_run_is_a_404(client):
    assert client.get("/api/screenings/does-not-exist/export").status_code == 404


def test_export_requires_a_session(monkeypatch):
    """Gated at the same rung as the report (AC 4): a caller who may not read the
    report must not read the cohort by asking for it as a spreadsheet."""
    with TestClient(main.app, raise_server_exceptions=False) as anonymous:
        response = anonymous.get("/api/screenings/any/export")
    assert response.status_code == 401


async def test_exporting_a_cohort_logs_who_took_the_copy(capsys):
    """This file carries patient data out of the app, so the download is attributed
    in the log — and only there: replaying a run must not write to it. The org-wide
    audit index (#98) is built off this stream (AC 4).

    Format-agnostic, like the other log assertions: the event name and the email
    appear under both renderers.
    """
    from app.persistence import InMemoryScreeningStore

    store = InMemoryScreeningStore()
    thread_id = await screening.create_screening(store, "p.txt", b"text")
    graph = FakeGraph(FakeSnapshot(payload()["values"]))

    await screening.get_screening_export(store, graph, thread_id, "json", REVIEWER)

    exported = [
        line for line in capsys.readouterr().out.splitlines() if "screening.cohort_exported" in line
    ]
    assert exported, "expected a screening.cohort_exported log line"
    assert REVIEWER.email in exported[0]
    assert "json" in exported[0]


async def test_the_attribution_line_names_no_patient(capsys):
    """The log is the one place this feature writes to, and it is read by people
    who are not cleared for the cohort. It records who exported and how many rows
    left — never which patients they were."""
    from app.persistence import InMemoryScreeningStore

    store = InMemoryScreeningStore()
    thread_id = await screening.create_screening(store, "p.txt", b"text")
    graph = FakeGraph(FakeSnapshot(payload()["values"]))

    await screening.get_screening_export(store, graph, thread_id, "csv", REVIEWER)

    exported = next(
        line for line in capsys.readouterr().out.splitlines() if "screening.cohort_exported" in line
    )
    for evaluation in COHORT:
        assert evaluation["patient_id"] not in exported
        assert evaluation["name"] not in exported
    # The volume is recorded — that is the fact an auditor wants — as a bare count.
    # Asserted on the field name alone: the console renderer colorizes the `=`, so
    # `patients=3` is not a literal substring under both renderers.
    assert "patients" in exported
