"""Screenability / coverage per run (#93).

Five halves, in this order: `app/services/coverage.py` as a pure reduction over a
checkpoint, the cross-run aggregate that turns it into a vocabulary backlog, the
section it renders into the exported report, the `/state` payload that serves it —
to the run detail view and to the gate — and the runs index row it is denormalized
into.

The fixture is one extraction of six criteria and two sentences the Parser refused,
scored against a cohort where one criterion comes back `unknown` for everybody. So
every figure below is checkable by hand: 6 structured of 8, one of those six never
resolved, therefore 5 of 8 checkable. The cohort is built with the Matcher's own
eligibility rule rather than hardcoded flags, so nothing here quietly assumes what
`services/cohort.py` would say.
"""

from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.persistence import InMemoryScreeningStore
from app.services import coverage, report, screening
from tests.auth_helpers import REVIEWER, sign_in

AGE: dict[str, Any] = {
    "attribute": "age",
    "operator": ">=",
    "value": 18.0,
    "value_high": None,
    "unit": "years",
    "source_text": "Age 18 years or older at the time of consent.",
}
EGFR: dict[str, Any] = {
    "attribute": "egfr",
    "operator": ">=",
    "value": 60.0,
    "value_high": None,
    "unit": "mL/min/1.73m2",
    "source_text": "eGFR at least 60 mL/min/1.73m2.",
}
ECOG: dict[str, Any] = {
    "attribute": "ecog",
    "operator": "<=",
    "value": 1.0,
    "value_high": None,
    "unit": "",
    "source_text": "ECOG performance status of 0 or 1.",
}
NSCLC: dict[str, Any] = {
    "category": "diagnosis",
    "value": "NSCLC",
    "negated": False,
    "source_text": "Histologically confirmed non-small cell lung cancer.",
}
# The criterion no patient record can answer — `unknown` for the whole cohort, and
# so the run's one *resolution* gap as opposed to its parse gaps.
BIOMARKER: dict[str, Any] = {
    "category": "biomarker",
    "value": "PD-L1 TPS >= 50%",
    "negated": False,
    "source_text": "PD-L1 tumour proportion score of 50% or higher.",
}
INFECTION: dict[str, Any] = {
    "category": "condition",
    "value": "active infection",
    "negated": False,
    "source_text": "Any active systemic infection requiring treatment.",
}

# Two sentences the Parser would not invent structure for. The first is the
# phrasing the aggregate below ranks; the second is unique to this protocol.
ORGAN_FUNCTION = "Adequate bone marrow and organ function per investigator assessment."
INVESTIGATOR = "Any condition that in the investigator's opinion would compromise safety."

CRITERIA: dict[str, Any] = {
    "trial_title": "Coverage Trial",
    "inclusion_quantitative": [AGE, EGFR, ECOG],
    "inclusion_categorical": [NSCLC, BIOMARKER],
    "exclusion_quantitative": [],
    "exclusion_categorical": [INFECTION],
    "unparseable": [ORGAN_FUNCTION, INVESTIGATOR],
}

BIOMARKER_LABEL = "PD-L1 TPS >= 50% (biomarker)"

# `(criterion, kind)` in the order the Matcher writes them.
APPLIED = (
    (AGE, "inclusion"),
    (EGFR, "inclusion"),
    (ECOG, "inclusion"),
    (NSCLC, "inclusion"),
    (BIOMARKER, "inclusion"),
    (INFECTION, "exclusion"),
)


def _evaluation(patient_id: str, statuses: dict[str, str]) -> dict[str, Any]:
    """One patient's evaluation, shaped exactly as the Matcher writes it.

    `statuses` names the criteria that did anything other than pass; the
    eligibility flags are derived with `evaluate_patient`'s own two lines rather
    than declared, so a test that reconciles against the cohort buckets is a real
    check and not two copies of one assumption.
    """
    results = [
        {
            "criterion": criterion,
            "kind": kind,
            "status": statuses.get(str(criterion.get("attribute") or criterion["value"]), "pass"),
            "explanation": "…",
        }
        for criterion, kind in APPLIED
    ]
    known = [result for result in results if result["status"] != "unknown"]
    return {
        "patient_id": patient_id,
        "name": patient_id.lower(),
        "eligible": bool(known) and all(result["status"] == "pass" for result in known),
        "needs_review": any(result["status"] == "unknown" for result in results),
        "criterion_results": results,
        "summary": f"{patient_id} was screened.",
    }


# Four patients. The biomarker is indeterminate for all of them — no record carries
# it — while eGFR and ECOG are settled for everyone even though they fail some.
COHORT: list[dict[str, Any]] = [
    _evaluation("PT-1", {"PD-L1 TPS >= 50%": "unknown"}),
    _evaluation("PT-2", {"PD-L1 TPS >= 50%": "unknown", "egfr": "fail"}),
    _evaluation("PT-3", {"PD-L1 TPS >= 50%": "unknown", "ecog": "fail"}),
    _evaluation("PT-4", {"PD-L1 TPS >= 50%": "unknown"}),
]

SCORED = {"parsed_criteria": CRITERIA, "matched_patients": COHORT}
AT_THE_GATE = {"parsed_criteria": CRITERIA}


# --- The reduction ----------------------------------------------------------


def test_the_score_is_checkable_criteria_over_every_criterion_the_protocol_yielded():
    """The headline figure: 5 of 8, not 5 of 6 and not 6 of 8.

    Six criteria were structured out of eight the protocol yielded, and one of the
    six could not be evaluated for anybody — so five were actually checked. A score
    over the six would hide the two sentences; a score counting the biomarker would
    claim a check nobody could make.
    """
    score = coverage.build_coverage(SCORED)
    assert (score["structured"], score["unparseable"]) == (6, 2)
    assert (score["resolved"], score["unresolved"]) == (5, 1)
    assert (score["checkable"], score["criteria"]) == (5, 8)
    assert score["score"] == 62.5


def test_the_two_layers_are_reported_apart():
    """A vocabulary gap and a data gap are different work, so they are two figures."""
    score = coverage.build_coverage(SCORED)
    assert score["parse_score"] == 75.0  # 6 of 8 structured
    assert score["match_score"] == pytest.approx(83.3)  # 5 of 6 resolved


def test_a_criterion_the_matcher_settled_for_anyone_counts_as_resolved():
    """eGFR failed two patients and passed two — either way the run checked it.

    "Resolved" is not "passed": a criterion that excluded someone is a criterion
    the protocol was screened on, which is exactly what coverage measures.
    """
    score = coverage.build_coverage(SCORED)
    labels = [gap["text"] for gap in score["gaps"] if gap["reason"] == coverage.UNRESOLVED]
    assert labels == [BIOMARKER_LABEL]


def test_a_gap_names_the_sentence_or_the_criterion_it_came_from():
    """Each gap is findable in the criteria table: a verbatim sentence, or a label."""
    gaps = coverage.build_coverage(SCORED)["gaps"]
    assert gaps == [
        {"reason": "unparseable", "text": ORGAN_FUNCTION, "kind": "", "patients": 0},
        {"reason": "unparseable", "text": INVESTIGATOR, "kind": "", "patients": 0},
        {
            "reason": "unresolved",
            "text": BIOMARKER_LABEL,
            "kind": "inclusion",
            # Every patient in the cohort, because no record answered it.
            "patients": 4,
        },
    ]


def test_unparseable_sentences_come_first_and_keep_the_extraction_order():
    """They are the reviewer's check-by-hand list, and the report prints the same
    order — two renderings of one extraction must not shuffle it."""
    gaps = coverage.build_coverage(SCORED)["gaps"]
    assert [gap["reason"] for gap in gaps] == ["unparseable", "unparseable", "unresolved"]
    assert [gap["text"] for gap in gaps[:2]] == [ORGAN_FUNCTION, INVESTIGATOR]


def _categorical_extraction(*criteria: dict[str, Any]) -> dict[str, Any]:
    """An extraction of nothing but these categorical criteria."""
    return {
        "inclusion_categorical": list(criteria),
        "inclusion_quantitative": [],
        "exclusion_quantitative": [],
        "exclusion_categorical": [],
        "unparseable": [],
    }


def test_unresolved_criteria_are_ranked_by_the_patients_they_cost():
    """Most expensive first: the criterion the Matcher could not settle for anybody
    is a bigger gap than one it could not settle for a single patient.

    The second criterion is absent from two of the three evaluations, which is what
    a cohort scored against an earlier extraction looks like — it still cost the one
    patient who carried it, and the row says so rather than claiming the cohort.
    """
    partial = [
        {
            "patient_id": f"PT-{index}",
            "eligible": False,
            "needs_review": True,
            "criterion_results": [
                {"criterion": NSCLC, "kind": "inclusion", "status": "unknown"},
                *(
                    [{"criterion": BIOMARKER, "kind": "inclusion", "status": "unknown"}]
                    if index == 0
                    else []
                ),
            ],
        }
        for index in range(3)
    ]
    score = coverage.build_coverage(
        {
            "parsed_criteria": _categorical_extraction(NSCLC, BIOMARKER),
            "matched_patients": partial,
        }
    )
    assert [(gap["text"], gap["patients"]) for gap in score["gaps"]] == [
        ("NSCLC (diagnosis)", 3),
        (BIOMARKER_LABEL, 1),
    ]


def test_the_gap_order_is_deterministic_when_two_criteria_cost_the_same():
    """Ties break on the label, so two exports of one run list them in one order
    rather than in whatever order the checkpoint happened to be walked in."""
    first, second = dict(NSCLC, value="zeta"), dict(NSCLC, value="alpha")
    tied = [
        {
            "patient_id": "PT-1",
            "eligible": False,
            "needs_review": True,
            "criterion_results": [
                {"criterion": first, "kind": "inclusion", "status": "unknown"},
                {"criterion": second, "kind": "inclusion", "status": "unknown"},
            ],
        }
    ]
    score = coverage.build_coverage(
        {
            "parsed_criteria": _categorical_extraction(first, second),
            "matched_patients": tied,
        }
    )
    assert [gap["text"] for gap in score["gaps"]] == [
        "alpha (diagnosis)",
        "zeta (diagnosis)",
    ]


def test_before_matching_the_score_is_the_parse_layer_and_says_so():
    """The gate's reading (AC 3). A run with no cohort has not failed to resolve
    anything, so `checkable` is every structured criterion and `match_score` is
    absent rather than 0 — which would read as a catastrophe."""
    score = coverage.build_coverage(AT_THE_GATE)
    assert score["scored"] is False
    assert score["match_score"] is None
    assert (score["checkable"], score["criteria"]) == (6, 8)
    assert score["score"] == score["parse_score"] == 75.0
    # And only the parse gaps are listed — nothing has been evaluated to fail.
    assert [gap["reason"] for gap in score["gaps"]] == ["unparseable", "unparseable"]


def test_a_fully_structured_and_resolved_run_scores_a_hundred():
    clean = {
        "parsed_criteria": {
            "inclusion_quantitative": [AGE],
            "inclusion_categorical": [],
            "exclusion_quantitative": [],
            "exclusion_categorical": [],
            "unparseable": [],
        },
        "matched_patients": [
            {
                "patient_id": "PT-1",
                "eligible": True,
                "needs_review": False,
                "criterion_results": [{"criterion": AGE, "kind": "inclusion", "status": "pass"}],
            }
        ],
    }
    score = coverage.build_coverage(clean)
    assert (score["score"], score["checkable"], score["criteria"]) == (100.0, 1, 1)
    assert score["gaps"] == []


def test_a_criterion_the_cohort_carries_no_result_for_is_not_resolved():
    """A structured criterion the Matcher never applied is as uncheckable as one it
    could not settle — counting it as resolved is the inflation this prevents."""
    score = coverage.build_coverage(
        {
            "parsed_criteria": {
                "inclusion_quantitative": [AGE, EGFR],
                "inclusion_categorical": [],
                "exclusion_quantitative": [],
                "exclusion_categorical": [],
                "unparseable": [],
            },
            "matched_patients": [
                {
                    "patient_id": "PT-1",
                    "eligible": True,
                    "needs_review": False,
                    # Only one of the two criteria was applied.
                    "criterion_results": [
                        {"criterion": AGE, "kind": "inclusion", "status": "pass"}
                    ],
                }
            ],
        }
    )
    assert (score["resolved"], score["unresolved"]) == (1, 1)
    assert [gap["text"] for gap in score["gaps"]] == ["egfr >= 60 mL/min/1.73m2"]


def test_an_unrecognized_status_is_not_a_resolution():
    """Reading a verdict this build does not know as settled would inflate the one
    figure this module exists to keep honest."""
    score = coverage.build_coverage(
        {
            "parsed_criteria": {
                "inclusion_quantitative": [AGE],
                "inclusion_categorical": [],
                "exclusion_quantitative": [],
                "exclusion_categorical": [],
                "unparseable": [],
            },
            "matched_patients": [
                {
                    "patient_id": "PT-1",
                    "eligible": False,
                    "needs_review": True,
                    "criterion_results": [
                        {"criterion": AGE, "kind": "inclusion", "status": "indeterminate"}
                    ],
                }
            ],
        }
    )
    assert (score["resolved"], score["unresolved"], score["score"]) == (0, 1, 0.0)


def test_a_criterion_extracted_twice_counts_twice_in_the_denominator():
    """The denominator is the criteria table a reviewer counts, and a requirement
    quoted from two sentences is two rows there. Both share one verdict, because
    they are one criterion as far as the Matcher is concerned."""
    doubled = dict(
        CRITERIA, inclusion_categorical=[BIOMARKER, dict(BIOMARKER, source_text="Also.")]
    )
    score = coverage.build_coverage({"parsed_criteria": doubled, "matched_patients": COHORT})
    assert score["structured"] == 6
    assert score["unresolved"] == 2
    # And the gap is listed once per row, since each row is a row of the table.
    assert sum(1 for gap in score["gaps"] if gap["reason"] == coverage.UNRESOLVED) == 2


def test_structured_count_is_the_runs_index_criteria_column():
    """One definition of "criteria found", shared with the store column (#51)."""
    assert coverage.structured_count(SCORED) == coverage.build_coverage(SCORED)["structured"] == 6


def test_a_score_of_nothing_is_zero_not_a_hundred():
    """A run with no extraction has no coverage — the views render nothing at all."""
    score = coverage.build_coverage({})
    assert (score["criteria"], score["score"], score["gaps"]) == (0, 0.0, [])
    assert score["scored"] is False


@pytest.mark.parametrize(
    "values",
    [
        {"parsed_criteria": None},
        {"parsed_criteria": "not an extraction"},
        {"parsed_criteria": {"inclusion_quantitative": "not a list"}},
        {"parsed_criteria": {"unparseable": "a string is not a list of sentences"}},
        {"parsed_criteria": {"unparseable": ["", "   "]}},
        {"parsed_criteria": CRITERIA, "matched_patients": "not a cohort"},
        {"parsed_criteria": CRITERIA, "matched_patients": [None, 7]},
        {"parsed_criteria": CRITERIA, "matched_patients": [{"criterion_results": [None]}]},
    ],
)
def test_a_malformed_checkpoint_degrades_instead_of_raising(values):
    """These payloads come off a checkpoint an older build — or a hand — wrote."""
    score = coverage.build_coverage(values)
    assert score["checkable"] <= score["criteria"]
    assert 0.0 <= score["score"] <= 100.0


def test_blank_unparseable_entries_are_not_counted_as_criteria():
    """An empty string is not a criterion nobody screened on; counting one would
    both inflate the denominator and put an unreadable row at the top of the list."""
    score = coverage.build_coverage({"parsed_criteria": dict(CRITERIA, unparseable=["", "  "])})
    assert (score["unparseable"], score["criteria"]) == (0, 6)
    assert score["gaps"] == []


# --- Across runs: the vocabulary backlog (AC 4) -----------------------------


def _run(unparseable: list[str], *, structured: int = 2) -> coverage.Coverage:
    """One run's coverage, built from a real extraction rather than hand-written."""
    return coverage.build_coverage(
        {
            "parsed_criteria": {
                "inclusion_quantitative": [AGE, EGFR][:structured],
                "inclusion_categorical": [],
                "exclusion_quantitative": [],
                "exclusion_categorical": [],
                "unparseable": unparseable,
            }
        }
    )


def test_the_aggregate_pools_coverage_rather_than_averaging_scores():
    """A two-criterion protocol must not swing the instance's figure as far as a
    forty-criterion one, so the score is checkable over criteria across the whole
    window."""
    pooled = coverage.aggregate([_run([ORGAN_FUNCTION]), _run([], structured=2)])
    # 4 structured of 5 criteria across the two runs.
    assert (pooled["checkable"], pooled["criteria"]) == (4, 5)
    assert pooled["score"] == 80.0
    assert (pooled["runs"], pooled["sampled"]) == (2, 2)


def test_the_aggregate_ranks_the_phrasings_by_the_runs_they_appear_in():
    """The backlog question is "which wording should the vocabulary swallow next",
    and that is the wording that keeps coming back — not the longest list."""
    pooled = coverage.aggregate(
        [
            _run([ORGAN_FUNCTION, INVESTIGATOR]),
            _run([ORGAN_FUNCTION]),
            _run([ORGAN_FUNCTION]),
        ]
    )
    assert [(phrase["text"], phrase["runs"], phrase["count"]) for phrase in pooled["phrases"]] == [
        (ORGAN_FUNCTION, 3, 3),
        (INVESTIGATOR, 1, 1),
    ]
    # Shares are of every unparseable sentence in the window, as the panel says.
    assert pooled["phrases"][0]["share"] == 75.0


def test_the_phrase_ranking_orders_by_the_count_its_share_is_computed_from():
    """A caller draws a bar per row from `share`, so a ranking on anything else
    would render bars that get longer as the list goes down."""
    pooled = coverage.aggregate([_run([ORGAN_FUNCTION, ORGAN_FUNCTION]), _run([INVESTIGATOR])])
    assert [(phrase["count"], phrase["runs"]) for phrase in pooled["phrases"]] == [(2, 1), (1, 1)]
    shares = [phrase["share"] for phrase in pooled["phrases"]]
    assert shares == sorted(shares, reverse=True)


def test_phrasings_group_case_and_whitespace_insensitively():
    """Two uploads of one protocol differ in line wrapping far more often than in
    wording, and two spellings of one phrasing would both fall below the cap."""
    rewrapped = "Adequate bone marrow  and organ function\nper investigator assessment."
    pooled = coverage.aggregate([_run([ORGAN_FUNCTION]), _run([rewrapped.upper()])])
    assert len(pooled["phrases"]) == 1
    assert pooled["phrases"][0]["runs"] == 2
    # Displayed as the first (newest) run wrote it, not as the older one did.
    assert pooled["phrases"][0]["text"] == ORGAN_FUNCTION


def test_the_ranking_is_capped_and_the_payload_says_how_much_it_left_out():
    """A truncated list that did not say so would read as the whole backlog."""
    wide = coverage.aggregate([_run([f"Vague requirement {index}." for index in range(20)])])
    assert len(wide["phrases"]) == coverage.PHRASE_DEPTH
    assert wide["phrasings"] == 20


def test_the_aggregate_states_its_own_window():
    """A sample that read as the whole history would send someone off to implement
    the wrong attribute."""
    pooled = coverage.aggregate([_run([ORGAN_FUNCTION])], total=312)
    assert (pooled["sampled"], pooled["total"]) == (1, 312)


def test_runs_with_no_extraction_count_towards_the_window_and_nothing_else():
    pooled = coverage.aggregate([_run([ORGAN_FUNCTION]), coverage.build_coverage({})])
    assert (pooled["sampled"], pooled["runs"]) == (2, 1)
    assert pooled["criteria"] == 3


def test_an_empty_window_is_a_zero_aggregate_not_a_division_error():
    pooled = coverage.aggregate([])
    assert (pooled["runs"], pooled["score"], pooled["phrases"]) == (0, 0.0, [])


# --- The report section (#56) ------------------------------------------------


def _report_html(**values: Any) -> str:
    return report.render_report({"values": {"current_step": "done", **values}})


def test_report_leads_with_what_could_not_be_checked():
    html = _report_html(**SCORED)
    assert 'data-region="report-coverage"' in html
    assert "Screenability" in html
    assert "<strong>5 of 8 criteria</strong> could be checked" in html
    # The share is printed the way the app prints it, not as Python repr does.
    assert "62.5%" in html


def test_report_lists_every_gap_with_its_reason():
    html = _report_html(**SCORED)
    assert "Never structured" in html
    assert "Could not be evaluated" in html
    assert "Adequate bone marrow and organ function" in html
    assert "PD-L1 TPS &gt;= 50% (biomarker)" in html


def test_report_of_a_parked_run_says_the_figure_is_provisional():
    """Exported before matching, the parse layer is all that can be known — and a
    document implying the Matcher had settled these criteria would claim a check
    nobody ran."""
    html = _report_html(parsed_criteria=CRITERIA, current_step="awaiting_approval")
    assert "<strong>6 of 8 criteria</strong> were structured for checking" in html
    assert "No cohort has been scored yet" in html


def test_report_states_full_coverage_rather_than_dropping_the_section():
    """ "All 20 criteria were checkable" is a fact a handoff document should state."""
    html = _report_html(
        parsed_criteria={
            "inclusion_quantitative": [AGE],
            "inclusion_categorical": [],
            "exclusion_quantitative": [],
            "exclusion_categorical": [],
            "unparseable": [],
        },
        matched_patients=[
            {
                "patient_id": "PT-1",
                "eligible": True,
                "needs_review": False,
                "criterion_results": [{"criterion": AGE, "kind": "inclusion", "status": "pass"}],
            }
        ],
    )
    assert "<strong>1 of 1 criterion</strong>" in html
    assert "Every criterion in this extraction was structured and evaluated." in html


def test_a_clean_extraction_at_the_gate_does_not_claim_a_cohort_was_evaluated():
    """The full-coverage sentence is conditioned like the lead above it — a document
    that said "and evaluated" two lines after "no cohort has been scored" would
    contradict itself."""
    html = _report_html(
        parsed_criteria={
            "inclusion_quantitative": [AGE],
            "inclusion_categorical": [],
            "exclusion_quantitative": [],
            "exclusion_categorical": [],
            "unparseable": [],
        },
        current_step="awaiting_approval",
    )
    assert "none has been evaluated yet" in html
    assert "structured and evaluated" not in html


def test_report_drops_the_section_for_a_run_with_no_extraction():
    assert 'data-region="report-coverage"' not in _report_html(matched_patients=[])


def test_report_escapes_the_sentences_it_prints():
    """Every string here is downstream of an upload and an LLM."""
    html = _report_html(
        parsed_criteria=dict(CRITERIA, unparseable=["<script>alert(1)</script>"]),
    )
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_the_report_section_comes_before_the_criteria_it_is_a_share_of():
    """A reader who works through the criteria tables without knowing two sentences
    never became criteria has been told a flattering half of the story."""
    html = _report_html(**SCORED)
    assert html.index('data-region="report-coverage"') < html.index('data-region="report-criteria"')


# --- The /state payload, and the gate ---------------------------------------


PROTOCOL = (
    "Phase II single-arm study of an investigational agent in adults.\n\n"
    "Inclusion criteria:\n"
    "- Age 18 years or older at the time of consent.\n\n"
    "Exclusion criteria:\n"
    "- Any active systemic infection requiring treatment.\n"
)


class FakeSnapshot:
    def __init__(self, values: dict[str, Any], pending: tuple = ()):
        self.values = values
        self.next = pending


class FakeGraph:
    """Returns one fixed snapshot — reading coverage never runs the pipeline."""

    def __init__(self, snapshot: FakeSnapshot):
        self.snapshot = snapshot

    async def aget_state(self, _config: object) -> FakeSnapshot:
        return self.snapshot

    async def astream(  # pragma: no cover - coverage never drives the graph
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


def test_state_serves_the_coverage_beside_the_checkpoint(client, monkeypatch):
    """Derived server-side, on the payload the run detail view already fetches."""
    thread_id = _create(client)
    monkeypatch.setattr(main, "graph", FakeGraph(FakeSnapshot(SCORED)))

    body = client.get(f"/api/screenings/{thread_id}/state").json()

    assert body["coverage"]["checkable"] == 5
    assert body["coverage"]["criteria"] == 8
    assert body["coverage"]["score"] == 62.5
    # Derived, not stored: the extraction is still there untouched beside it.
    assert body["values"]["parsed_criteria"]["trial_title"] == "Coverage Trial"


def test_state_serves_coverage_for_a_run_parked_at_the_gate(client, monkeypatch):
    """AC 3: this payload is what the gate reads, so a reviewer sees the figure
    while the decision is still theirs."""
    thread_id = _create(client)
    monkeypatch.setattr(main, "graph", FakeGraph(FakeSnapshot(AT_THE_GATE, pending=("matcher",))))

    body = client.get(f"/api/screenings/{thread_id}/state").json()

    assert body["pending"] == ["matcher"]
    assert body["coverage"]["scored"] is False
    assert (body["coverage"]["checkable"], body["coverage"]["criteria"]) == (6, 8)
    assert body["coverage"]["match_score"] is None


def test_coverage_cannot_disagree_with_the_criteria_table_it_is_served_with(client, monkeypatch):
    """AC 5, as a property of one response: the denominator is exactly the rows the
    same payload carries."""
    thread_id = _create(client)
    monkeypatch.setattr(main, "graph", FakeGraph(FakeSnapshot(SCORED)))

    body = client.get(f"/api/screenings/{thread_id}/state").json()
    criteria = body["values"]["parsed_criteria"]

    structured = sum(len(criteria[bucket]) for bucket in coverage.CRITERIA_BUCKETS)
    assert body["coverage"]["structured"] == structured
    assert body["coverage"]["unparseable"] == len(criteria["unparseable"])
    assert body["coverage"]["criteria"] == structured + len(criteria["unparseable"])


def test_a_run_with_no_checkpoint_serves_an_empty_score(client, monkeypatch):
    thread_id = _create(client)
    monkeypatch.setattr(main, "graph", FakeGraph(FakeSnapshot({})))

    body = client.get(f"/api/screenings/{thread_id}/state").json()

    assert body["coverage"]["criteria"] == 0
    assert body["coverage"]["gaps"] == []


# --- The runs index row (AC 2) ----------------------------------------------


class TerminalGraph(FakeGraph):
    """A graph that produces no updates and lands on the fixture snapshot.

    Enough to drive `stream_screening` to its terminal frame — and `reject_screening`
    to its state write — which is where the run's summary columns are denormalized:
    the write this section is about.
    """

    async def astream(
        self, input: object, config: object = None, *, stream_mode: object = None
    ) -> AsyncIterator[dict]:
        return
        yield {}  # pragma: no cover - makes this an async generator

    async def aupdate_state(
        self, _config: object, _values: dict, as_node: str | None = None
    ) -> None:
        # Accepted and dropped: these tests are about the store row the caller
        # writes next, not about the checkpoint LangGraph would have written.
        return None


async def test_a_finished_run_denormalizes_its_coverage_into_the_index_row():
    """Written by the same terminal frame that records the status and the counts,
    so the index can render coverage for every row without loading a checkpoint."""
    store = InMemoryScreeningStore()
    thread_id = await screening.create_screening(store, "p.md", PROTOCOL.encode())

    frames = [
        frame
        async for frame in await screening.stream_screening(
            store, TerminalGraph(FakeSnapshot(SCORED)), thread_id
        )
    ]
    assert frames  # the terminal frame at least

    page = await screening.list_screenings(store, limit=10, offset=0)
    row = page["items"][0]
    # The "Criteria" column and the coverage denominator are one derivation.
    assert row["criteria_count"] == 6
    assert row["coverage"] == {"checkable": 5, "criteria": 8, "score": 62.5}


async def test_a_row_that_never_ran_reports_no_coverage():
    """An uploaded-but-never-streamed run has nothing to be a share of, and the
    index renders that as an em dash rather than as 0%."""
    store = InMemoryScreeningStore()
    await screening.create_screening(store, "p.md", PROTOCOL.encode())
    page = await screening.list_screenings(store, limit=10, offset=0)
    assert page["items"][0]["coverage"] == {"checkable": 0, "criteria": 0, "score": 0.0}


async def test_a_rejected_run_keeps_the_coverage_that_may_have_justified_it():
    """A protocol refused at the gate is often refused *because* half of it could
    not be checked, so its row must not lose the figure (#91 + #93)."""
    store = InMemoryScreeningStore()
    thread_id = await screening.create_screening(store, "p.md", PROTOCOL.encode())
    graph = TerminalGraph(FakeSnapshot(AT_THE_GATE, pending=("matcher",)))

    await screening.reject_screening(store, graph, thread_id, REVIEWER, "Only 6 of 8 checkable.")

    page = await screening.list_screenings(store, limit=10, offset=0)
    row = page["items"][0]
    assert row["status"] == "rejected"
    assert row["coverage"] == {"checkable": 6, "criteria": 8, "score": 75.0}


# --- The metrics summary (AC 4, over the API) -------------------------------


def test_the_metrics_summary_aggregates_coverage_across_runs(client, monkeypatch):
    """Every run in the window is read from its own checkpoint, so the aggregate is
    the same derivation the per-run panels show."""
    _create(client)
    _create(client)
    monkeypatch.setattr(main, "graph", FakeGraph(FakeSnapshot(SCORED)))

    body = client.get("/api/metrics/summary").json()

    aggregate = body["coverage"]
    assert (aggregate["sampled"], aggregate["runs"], aggregate["total"]) == (2, 2, 2)
    # Both runs read the same fixture snapshot: 10 checkable of 16.
    assert (aggregate["checkable"], aggregate["criteria"]) == (10, 16)
    assert aggregate["score"] == 62.5
    assert [phrase["text"] for phrase in aggregate["phrases"]] == [ORGAN_FUNCTION, INVESTIGATOR]
    assert aggregate["phrases"][0]["runs"] == 2


def test_the_metrics_summary_survives_an_instance_with_no_runs(client):
    body = client.get("/api/metrics/summary").json()
    assert body["coverage"]["runs"] == 0
    assert body["coverage"]["phrases"] == []


def test_one_unreadable_checkpoint_costs_its_own_run_and_not_the_page(client, monkeypatch):
    """The three counter panels need no I/O at all, and this endpoint could not fail
    before coverage was added to it. A corrupt row narrows the window instead."""

    class HalfBrokenGraph(FakeGraph):
        def __init__(self) -> None:
            super().__init__(FakeSnapshot(SCORED))
            self.reads = 0

        async def aget_state(self, _config: object) -> FakeSnapshot:
            self.reads += 1
            if self.reads == 1:
                raise RuntimeError("checkpoint blob is not readable")
            return self.snapshot

    _create(client)
    _create(client)
    graph = HalfBrokenGraph()
    monkeypatch.setattr(main, "graph", graph)

    response = client.get("/api/metrics/summary")

    assert response.status_code == 200
    body = response.json()
    # The collector-backed blocks still answer (their counters are process-wide, so
    # the figures belong to the whole suite — what matters is that they arrived).
    assert set(body) >= {"funnel", "rejections", "attempts", "coverage"}
    # One of the two runs was skipped, and the window says so: sampled < total.
    assert (body["coverage"]["sampled"], body["coverage"]["total"]) == (1, 2)
    assert body["coverage"]["checkable"] == 5
