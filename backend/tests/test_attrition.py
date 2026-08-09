"""Per-criterion cohort attrition (#94).

Three halves: `app/services/attrition.py` as a pure reduction over a checkpoint's
`matched_patients`, the section it renders into the exported report, and the
`/state` payload that serves it to the run detail view.

The fixture below is one cohort designed so every figure is checkable by hand —
two criteria that overlap, a patient whose only failure is recoverable, a patient
whose is not because something else about them is indeterminate, and a criterion
that excludes nobody. `_evaluation` derives `eligible`/`needs_review` with the
Matcher's own rule (`graph/nodes/matcher.evaluate_patient`) rather than hardcoding
them, so the reconciliation test below is a real check against
`services/cohort.py` and not two copies of one assumption.
"""

from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.services import attrition, cohort, report
from tests.auth_helpers import sign_in

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
INFECTION: dict[str, Any] = {
    "category": "condition",
    "value": "active infection",
    "negated": False,
    "source_text": "Any active systemic infection requiring treatment.",
}

# `(criterion, kind)` in the order the Matcher writes them: quantitative
# inclusions, categorical inclusions, then the exclusions.
APPLIED_CRITERIA = (
    (AGE, "inclusion"),
    (EGFR, "inclusion"),
    (ECOG, "inclusion"),
    (NSCLC, "inclusion"),
    (INFECTION, "exclusion"),
)

AGE_KEY = "inclusion:age >= 18 years"
EGFR_KEY = "inclusion:egfr >= 60 mL/min/1.73m2"
ECOG_KEY = "inclusion:ecog <= 1"
NSCLC_KEY = "inclusion:NSCLC (diagnosis)"
INFECTION_KEY = "exclusion:active infection (condition)"


def _evaluation(patient_id: str, statuses: dict[str, str]) -> dict[str, Any]:
    """One patient's evaluation, shaped exactly as the Matcher writes it.

    `statuses` names the criteria that did anything other than pass, keyed by the
    criterion's `value`/`attribute`; everything else passes. The eligibility flags
    are derived, not declared — the same two lines `evaluate_patient` ends with.
    """
    results = [
        {
            "criterion": criterion,
            "kind": kind,
            "status": statuses.get(str(criterion.get("attribute") or criterion["value"]), "pass"),
            "explanation": "…",
        }
        for criterion, kind in APPLIED_CRITERIA
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


# 8 patients: eGFR excludes 4, ECOG 3 (two of them shared with eGFR), the
# infection exclusion 1, NSCLC could not be determined for 2, and age excludes
# nobody. One patient clears everything.
COHORT: list[dict[str, Any]] = [
    _evaluation("PT-1", {"egfr": "fail"}),
    _evaluation("PT-2", {"egfr": "fail"}),
    _evaluation("PT-3", {"egfr": "fail", "ecog": "fail"}),
    _evaluation("PT-4", {"egfr": "fail", "ecog": "fail"}),
    # Failing one criterion *and* indeterminate on another: relaxing ECOG moves
    # this patient to "needs review", never to eligible.
    _evaluation("PT-5", {"ecog": "fail", "NSCLC": "unknown"}),
    _evaluation("PT-6", {"active infection": "fail"}),
    _evaluation("PT-7", {}),
    _evaluation("PT-8", {"NSCLC": "unknown"}),
]


def _by_key(breakdown: attrition.CohortAttrition) -> dict[str, attrition.CriterionAttrition]:
    return {row["key"]: row for row in breakdown["criteria"]}


# --- The reduction ----------------------------------------------------------


def test_criteria_are_ranked_by_exclusions_most_restrictive_first():
    breakdown = attrition.build_attrition({"matched_patients": COHORT})
    assert [row["key"] for row in breakdown["criteria"]] == [
        EGFR_KEY,  # 4
        ECOG_KEY,  # 3
        INFECTION_KEY,  # 1
        # Neither excludes anyone; the one the Matcher could not evaluate ranks
        # above the one that simply passed everybody.
        NSCLC_KEY,
        AGE_KEY,
    ]


def test_every_criterion_is_listed_including_the_ones_that_excluded_nobody():
    """ "age >= 18 excluded 0" is a fact about the protocol, not a row to drop."""
    breakdown = attrition.build_attrition({"matched_patients": COHORT})
    age = _by_key(breakdown)[AGE_KEY]
    assert (age["excluded"], age["unresolved"], age["passed"]) == (0, 0, 8)


def test_exclusion_counts_and_shares_are_of_the_whole_cohort():
    rows = _by_key(attrition.build_attrition({"matched_patients": COHORT}))
    assert rows[EGFR_KEY]["excluded"] == 4
    assert rows[EGFR_KEY]["share"] == 50.0
    assert rows[ECOG_KEY]["excluded"] == 3
    assert rows[ECOG_KEY]["share"] == 37.5
    assert rows[INFECTION_KEY]["excluded"] == 1
    assert rows[INFECTION_KEY]["share"] == 12.5


def test_each_criterion_partitions_the_cohort_it_was_applied_to():
    """`excluded + unresolved + passed` is a patient count, for every row."""
    breakdown = attrition.build_attrition({"matched_patients": COHORT})
    for row in breakdown["criteria"]:
        assert row["excluded"] + row["unresolved"] + row["passed"] == 8, row["label"]


def test_unresolved_counts_criteria_the_matcher_could_not_evaluate():
    rows = _by_key(attrition.build_attrition({"matched_patients": COHORT}))
    assert rows[NSCLC_KEY]["unresolved"] == 2
    assert rows[NSCLC_KEY]["excluded"] == 0


def test_shared_exclusions_are_split_out_from_unique_ones():
    """The 41-of-which-19-are-shared problem: both criteria report the overlap."""
    rows = _by_key(attrition.build_attrition({"matched_patients": COHORT}))
    # PT-1 and PT-2 fail eGFR alone; PT-3 and PT-4 fail it alongside ECOG.
    assert (rows[EGFR_KEY]["unique"], rows[EGFR_KEY]["shared"]) == (2, 2)
    # PT-5 is ECOG's only sole failure.
    assert (rows[ECOG_KEY]["unique"], rows[ECOG_KEY]["shared"]) == (1, 2)
    for row in rows.values():
        assert row["unique"] + row["shared"] == row["excluded"], row["label"]


def test_recoverable_excludes_patients_who_would_still_need_a_human():
    """Relaxing a criterion only promises the patients it would make *eligible*.

    ECOG is the sole failure for PT-5, so it is `unique` to it — but PT-5's NSCLC
    status is indeterminate, so dropping ECOG moves them into the review bucket
    rather than the cohort. Reporting 1 there would be the false delta.
    """
    rows = _by_key(attrition.build_attrition({"matched_patients": COHORT}))
    assert rows[ECOG_KEY]["unique"] == 1
    assert rows[ECOG_KEY]["recoverable"] == 0
    # eGFR's two sole failures have nothing else outstanding.
    assert rows[EGFR_KEY]["recoverable"] == 2
    for row in rows.values():
        assert row["recoverable"] <= row["unique"], row["label"]


def test_overlap_is_reported_for_pairs_that_actually_share_patients():
    breakdown = attrition.build_attrition({"matched_patients": COHORT})
    assert breakdown["overlaps"] == [
        {
            "a_key": EGFR_KEY,
            "b_key": ECOG_KEY,
            "a_label": "egfr >= 60 mL/min/1.73m2",
            "b_label": "ecog <= 1",
            "patients": 2,
        }
    ]


def test_overlap_omits_pairs_with_nothing_in_common():
    """A table of zeros answers nothing — only the double-counting is reported."""
    breakdown = attrition.build_attrition({"matched_patients": COHORT})
    pairs = {(overlap["a_key"], overlap["b_key"]) for overlap in breakdown["overlaps"]}
    assert (EGFR_KEY, INFECTION_KEY) not in pairs
    assert (ECOG_KEY, INFECTION_KEY) not in pairs


def test_overlap_is_bounded_to_the_top_criteria():
    """Every pair of a twenty-criterion protocol is 190 figures nobody asked for."""
    criteria = [
        (
            {
                "attribute": f"lab{index}",
                "operator": ">=",
                "value": float(index),
                "value_high": None,
                "unit": "",
                "source_text": f"Lab {index} at least {index}.",
            },
            "inclusion",
        )
        for index in range(8)
    ]
    # One patient per criterion pair-set: every patient fails every criterion, so
    # every pair overlaps and only the depth cap can bound the result.
    everyone_fails = [
        {
            "patient_id": f"PT-{index}",
            "eligible": False,
            "needs_review": False,
            "criterion_results": [
                {"criterion": criterion, "kind": kind, "status": "fail"}
                for criterion, kind in criteria
            ],
        }
        for index in range(3)
    ]
    breakdown = attrition.build_attrition({"matched_patients": everyone_fails})
    assert len(breakdown["criteria"]) == 8
    depth = attrition.OVERLAP_DEPTH
    assert len(breakdown["overlaps"]) == depth * (depth - 1) // 2


def test_ranking_is_deterministic_when_counts_tie():
    """Two criteria excluding the same number sort by label, not by insertion."""
    first, second = dict(EGFR, attribute="zeta"), dict(EGFR, attribute="alpha")
    tied = [
        {
            "patient_id": "PT-1",
            "eligible": False,
            "needs_review": False,
            "criterion_results": [
                {"criterion": first, "kind": "inclusion", "status": "fail"},
                {"criterion": second, "kind": "inclusion", "status": "fail"},
            ],
        }
    ]
    breakdown = attrition.build_attrition({"matched_patients": tied})
    assert [row["label"] for row in breakdown["criteria"]] == [
        "alpha >= 60 mL/min/1.73m2",
        "zeta >= 60 mL/min/1.73m2",
    ]


def test_a_criterion_extracted_twice_counts_each_patient_once():
    """Duplicate rows merge, and the merged row is still a patient count."""
    doubled = [
        {
            "patient_id": "PT-1",
            "eligible": False,
            "needs_review": False,
            "criterion_results": [
                {"criterion": EGFR, "kind": "inclusion", "status": "fail"},
                # The same criterion, quoted from a second sentence.
                {
                    "criterion": dict(EGFR, source_text="Renal: eGFR >= 60."),
                    "kind": "inclusion",
                    "status": "pass",
                },
            ],
        }
    ]
    breakdown = attrition.build_attrition({"matched_patients": doubled})
    assert len(breakdown["criteria"]) == 1
    row = breakdown["criteria"][0]
    # Worst status wins: the criterion did exclude this patient.
    assert (row["excluded"], row["unresolved"], row["passed"]) == (1, 0, 0)
    # And the first provenance seen is the one shown, not both concatenated.
    assert row["source_text"] == EGFR["source_text"]


# --- Reconciliation with the cohort buckets ---------------------------------


def test_totals_are_the_cohort_buckets_verbatim():
    """The whole point of AC 5: not a fifth disagreeing rendering of one run."""
    totals = attrition.build_attrition({"matched_patients": COHORT})["totals"]
    buckets = cohort.bucket_counts(COHORT)
    assert (totals["eligible"], totals["review"], totals["ineligible"]) == (
        buckets["eligible"],
        buckets["review"],
        buckets["ineligible"],
    )
    assert totals["patients"] == sum(buckets.values()) == 8


def test_totals_decompose_the_buckets_without_double_counting_the_patients():
    totals = attrition.build_attrition({"matched_patients": COHORT})["totals"]
    # PT-7 alone clears everything; PT-5 and PT-8 have an indeterminate criterion.
    assert (totals["eligible"], totals["review"], totals["ineligible"]) == (1, 2, 5)
    # Six patients failed something — PT-5 among them, which is why `excluded` and
    # `review` are not disjoint and neither is a bucket.
    assert totals["excluded"] == 6
    assert totals["unresolved"] == 2
    assert totals["unscored"] == 0


def test_no_criterion_claims_a_patient_the_cohort_calls_eligible():
    """An exclusion attributed to a patient in the eligible bucket is a defect."""
    breakdown = attrition.build_attrition({"matched_patients": COHORT})
    eligible = [evaluation for evaluation in COHORT if cohort.bucket_of(evaluation) == "eligible"]
    excluded_or_unresolved = sum(
        row["excluded"] + row["unresolved"] for row in breakdown["criteria"]
    )
    per_patient = sum(
        1
        for evaluation in COHORT
        for result in evaluation["criterion_results"]
        if result["status"] != "pass"
    )
    assert excluded_or_unresolved == per_patient
    for evaluation in eligible:
        assert all(result["status"] == "pass" for result in evaluation["criterion_results"])


def test_patients_no_criterion_was_applied_to_are_named_not_hidden():
    """A cohort scored against an empty extraction still has to add up."""
    unscored = [
        {"patient_id": "PT-1", "eligible": False, "needs_review": False, "criterion_results": []},
        _evaluation("PT-2", {"egfr": "fail"}),
    ]
    totals = attrition.build_attrition({"matched_patients": unscored})["totals"]
    assert totals["patients"] == 2
    assert totals["unscored"] == 1
    assert totals["excluded"] == 1


# --- Degraded checkpoints ---------------------------------------------------


def test_a_run_with_no_cohort_yields_an_empty_breakdown():
    breakdown = attrition.build_attrition({})
    assert breakdown["criteria"] == []
    assert breakdown["overlaps"] == []
    assert breakdown["totals"]["patients"] == 0


@pytest.mark.parametrize(
    "values",
    [
        {"matched_patients": None},
        {"matched_patients": "not a cohort"},
        {"matched_patients": [None, 7, "x"]},
        {"matched_patients": [{"criterion_results": "not a list"}]},
        {"matched_patients": [{"criterion_results": [None]}]},
    ],
)
def test_a_malformed_checkpoint_degrades_instead_of_raising(values):
    """These payloads come off a checkpoint an older build may have written."""
    breakdown = attrition.build_attrition(values)
    assert breakdown["criteria"] == []


def test_an_unrecognized_status_counts_as_unevaluated_not_as_a_pass():
    """Reading a verdict this build does not know as a pass would inflate the cohort."""
    odd = [
        {
            "patient_id": "PT-1",
            "eligible": False,
            "needs_review": True,
            "criterion_results": [
                {"criterion": EGFR, "kind": "inclusion", "status": "indeterminate"},
            ],
        }
    ]
    row = attrition.build_attrition({"matched_patients": odd})["criteria"][0]
    assert (row["excluded"], row["unresolved"], row["passed"]) == (0, 1, 0)


# --- The report section (#56) -----------------------------------------------


def _report_html(**values: Any) -> str:
    return report.render_report({"values": {"current_step": "done", **values}})


def test_report_carries_the_attrition_breakdown():
    html = _report_html(matched_patients=COHORT)
    assert 'data-region="report-attrition"' in html
    assert "Cohort attrition" in html
    assert "egfr &gt;= 60 mL/min/1.73m2" in html
    # The lead states the two patient-level figures, pluralized.
    assert "Of 8 patients screened, 6 patients failed at least one criterion" in html


def test_report_prints_shares_the_way_the_app_prints_them():
    """`50%`, not Python's `50.0%` — this table and the panel are one derivation."""
    html = _report_html(matched_patients=COHORT)
    assert "50%" in html
    assert "50.0%" not in html
    # A share that genuinely has a decimal keeps it.
    assert "37.5%" in html


def test_report_prints_the_overlap_between_the_top_criteria():
    html = _report_html(matched_patients=COHORT)
    assert "Patients failing both" in html
    assert "egfr &gt;= 60 mL/min/1.73m2 + ecog &lt;= 1" in html


def test_the_overlap_heading_states_how_far_the_comparison_reached():
    """ "the top 5 criteria" on a run with three of them promises absent pairs."""
    html = _report_html(matched_patients=COHORT)
    # Three criteria excluded anyone here, so all three were compared.
    assert "Overlap between the criteria that excluded anyone" in html
    assert f"top {attrition.OVERLAP_DEPTH}" not in html


def test_the_overlap_heading_names_the_cap_when_the_cap_bites():
    """And when it does bite, the document says so rather than implying full cover."""
    wide = [
        {
            "patient_id": f"PT-{index}",
            "eligible": False,
            "needs_review": False,
            "criterion_results": [
                {
                    "criterion": dict(EGFR, attribute=f"lab{lab}"),
                    "kind": "inclusion",
                    "status": "fail",
                }
                for lab in range(attrition.OVERLAP_DEPTH + 2)
            ],
        }
        for index in range(2)
    ]
    html = _report_html(matched_patients=wide)
    assert f"Overlap between the {attrition.OVERLAP_DEPTH} most restrictive criteria" in html


def test_report_names_patients_no_criterion_accounts_for():
    html = _report_html(
        matched_patients=[
            {
                "patient_id": "PT-1",
                "eligible": False,
                "needs_review": False,
                "criterion_results": [],
            },
            _evaluation("PT-2", {"egfr": "fail"}),
        ]
    )
    assert "1 patient had no criteria applied at all" in html


def test_report_drops_the_section_for_a_run_with_no_cohort():
    """A parked or rejected run has nothing to attribute — no empty table."""
    assert 'data-region="report-attrition"' not in _report_html(parsed_criteria={})


def test_report_escapes_criterion_labels_from_the_protocol():
    """Every string here is downstream of an upload and an LLM."""
    injected = dict(NSCLC, value="<script>alert(1)</script>")
    html = _report_html(
        matched_patients=[
            {
                "patient_id": "PT-1",
                "eligible": False,
                "needs_review": False,
                "criterion_results": [
                    {"criterion": injected, "kind": "inclusion", "status": "fail"}
                ],
            }
        ]
    )
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


# --- The /state payload -----------------------------------------------------


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
    """Returns one fixed snapshot — reading attrition never runs the pipeline."""

    def __init__(self, snapshot: FakeSnapshot):
        self.snapshot = snapshot

    async def aget_state(self, _config: object) -> FakeSnapshot:
        return self.snapshot

    async def astream(  # pragma: no cover - attrition never drives the graph
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


def test_state_serves_the_attrition_beside_the_checkpoint(client, monkeypatch):
    """Derived server-side, on the payload the run detail view already fetches."""
    thread_id = _create(client)
    monkeypatch.setattr(main, "graph", FakeGraph(FakeSnapshot({"matched_patients": COHORT})))

    body = client.get(f"/api/screenings/{thread_id}/state").json()

    # Derived, not stored: the cohort is still there untouched, with the
    # breakdown alongside it.
    assert len(body["values"]["matched_patients"]) == 8
    assert body["attrition"]["totals"]["patients"] == 8
    assert body["attrition"]["criteria"][0]["key"] == EGFR_KEY
    assert body["attrition"]["overlaps"][0]["patients"] == 2


def test_a_run_with_no_cohort_serves_an_empty_breakdown(client, monkeypatch):
    """A run parked at the gate has no cohort to attribute, and says so in shape."""
    thread_id = _create(client)
    monkeypatch.setattr(main, "graph", FakeGraph(FakeSnapshot({})))

    body = client.get(f"/api/screenings/{thread_id}/state").json()

    assert body["attrition"] == {
        "totals": {
            "patients": 0,
            "eligible": 0,
            "review": 0,
            "ineligible": 0,
            "excluded": 0,
            "unresolved": 0,
            "unscored": 0,
        },
        "criteria": [],
        "overlaps": [],
    }
