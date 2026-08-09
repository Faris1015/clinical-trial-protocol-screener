"""What-if threshold simulation (#95).

Three halves again: `app/services/simulation.py` as a pure projection over a
checkpoint's cohort, the `POST /api/screenings/{id}/simulate` route that serves
it, and the promotion path that turns an accepted what-if into a real criteria
edit.

The fixture is deliberately *not* hand-written verdict rows. Every evaluation
below comes out of `matcher.evaluate_patient` against real patient records, so the
`observed` values the simulator re-applies thresholds to are the ones the Matcher
actually writes — a test that invented them could pass against a Matcher that had
stopped recording them at all.

The cohort is six patients chosen so every branch is checkable by hand: one who
clears everything, one whose only failure a relaxation fixes, one it doesn't reach,
one who stays in review because something else about them is undecidable, one with
no value on file at all, and one ruled out by an exclusion rather than an
inclusion.
"""

from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

import app.graph.nodes.critic as critic_mod
import app.graph.nodes.matcher as matcher_mod
import app.graph.nodes.parser as parser_mod
import app.main as main
from app.exceptions import InvalidSimulationError, ScreeningNotSimulatableError
from app.graph.nodes import matcher
from app.services import cohort, simulation
from tests.auth_helpers import sign_in
from tests.fakes import FAKE_PATIENTS, PROTOCOL_TEXT, FakeChatModel, good_criteria

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
PLATELETS: dict[str, Any] = {
    "attribute": "platelets",
    "operator": ">=",
    "value": 100.0,
    "value_high": None,
    "unit": "x10^9/L",
    "source_text": "Platelet count at least 100 x10^9/L.",
}
SBP: dict[str, Any] = {
    "attribute": "systolic_bp",
    "operator": ">=",
    "value": 160.0,
    "value_high": None,
    "unit": "mmHg",
    "source_text": "Uncontrolled hypertension (systolic BP >= 160 mmHg).",
}
NSCLC: dict[str, Any] = {
    "category": "diagnosis",
    "value": "NSCLC",
    "negated": False,
    "source_text": "Histologically confirmed non-small cell lung cancer.",
}

CRITERIA: dict[str, Any] = {
    "trial_title": "A trial",
    "inclusion_quantitative": [AGE, EGFR, PLATELETS],
    "inclusion_categorical": [NSCLC],
    "exclusion_quantitative": [SBP],
    "exclusion_categorical": [],
    "unparseable": [],
}

AGE_KEY = "inclusion:age >= 18 years"
EGFR_KEY = "inclusion:egfr >= 60 mL/min/1.73m2"
PLATELETS_KEY = "inclusion:platelets >= 100 x10^9/L"
SBP_KEY = "exclusion:systolic_bp >= 160 mmHg"
NSCLC_KEY = "inclusion:NSCLC (diagnosis)"

# The one term the fast path cannot settle, so the Matcher has to consult the
# cached verdict below — which is how a patient lands in "needs review" here.
AMBIGUOUS = "adenocarcinoma of the lung"
VERDICTS = {("nsclc", AMBIGUOUS): "uncertain"}


def _patient(patient_id: str, *, diagnosis: str = "NSCLC stage IV", **labs: float) -> dict:
    return {
        "id": patient_id,
        "name": patient_id.lower(),
        "labs": {"age": 40, "platelets": 200, "systolic_bp": 120, **labs},
        "diagnoses": [diagnosis],
        "medications": [],
        "history": [],
    }


PATIENTS = [
    # Clears everything.
    _patient("PT-1", egfr=70),
    # Fails eGFR alone, and nothing else about them is undecided: eGFR >= 50 makes
    # them eligible, eGFR >= 40 too.
    _patient("PT-2", egfr=55),
    # Fails eGFR by more than the relaxation under test reaches.
    _patient("PT-3", egfr=45),
    # Fails eGFR *and* has an undecidable diagnosis — relaxing eGFR moves them into
    # review, never into the cohort.
    _patient("PT-4", egfr=55, diagnosis=AMBIGUOUS),
    # No eGFR on file at all: undecidable at every threshold, and not a patient the
    # simulator failed to re-check.
    {**_patient("PT-5"), "labs": {"age": 40, "platelets": 200, "systolic_bp": 120}},
    # Ruled out by the exclusion, not by an inclusion.
    _patient("PT-6", egfr=70, systolic_bp=170),
]

COHORT = [matcher.evaluate_patient(patient, CRITERIA, VERDICTS) for patient in PATIENTS]
VALUES: dict[str, Any] = {
    "parsed_criteria": CRITERIA,
    "matched_patients": COHORT,
    "criteria_revision": 2,
}


def _override(key: str, value: float, operator: str = ">=", high: float | None = None):
    return simulation.Override(key=key, operator=operator, value=value, value_high=high)


def _buckets(breakdown) -> tuple[int, int, int]:
    totals = breakdown["totals"]
    return totals["eligible"], totals["review"], totals["ineligible"]


def _rows(breakdown) -> dict[str, Any]:
    return {row["key"]: row for row in breakdown["criteria"]}


# --- The fixture itself ------------------------------------------------------


def test_the_run_being_simulated_is_what_the_matcher_produced():
    """The baseline, stated once: everything below is a delta against these six."""
    assert _buckets(simulation.simulate(VALUES, [_override(EGFR_KEY, 50)])["current"]) == (1, 2, 3)
    assert cohort.bucket_counts(COHORT) == {"eligible": 1, "review": 2, "ineligible": 3}


def test_the_matcher_records_the_value_each_numeric_verdict_was_reached_from():
    """`observed` is the whole premise: without it nothing here can be re-derived."""
    egfr = next(
        result
        for result in COHORT[1]["criterion_results"]
        if result["criterion"] is EGFR and result["kind"] == "inclusion"
    )
    assert egfr["observed"] == 55
    assert egfr["status"] == "fail"
    # A patient with no value on file records the absence rather than omitting the
    # key — which is what separates "no lab" from "scored before #95".
    missing = next(
        result for result in COHORT[4]["criterion_results"] if result["criterion"] is EGFR
    )
    assert missing["observed"] is None
    assert missing["status"] == "unknown"


def test_categorical_verdicts_carry_no_observed_value():
    """There is nothing numeric to record, and a null there would read as one."""
    nsclc = next(
        result for result in COHORT[0]["criterion_results"] if result["criterion"] is NSCLC
    )
    assert "observed" not in nsclc


# --- Re-scoring --------------------------------------------------------------


def test_relaxing_a_threshold_moves_the_patients_it_reaches_and_no_others():
    result = simulation.simulate(VALUES, [_override(EGFR_KEY, 50)])
    # PT-2 alone crosses 50; PT-3 is still under it and PT-4 still has an
    # undecidable diagnosis.
    assert _buckets(result["simulated"]) == (2, 2, 2)
    assert result["delta"] == {"eligible": 1, "review": 0, "ineligible": -1}


def test_a_patient_with_something_else_undecided_moves_to_review_not_to_eligible():
    """The false delta #94 exists to prevent, now under simulation."""
    result = simulation.simulate(VALUES, [_override(EGFR_KEY, 40)])
    # PT-2 and PT-3 both clear 40. PT-4 clears it too and still needs a human.
    assert _buckets(result["simulated"]) == (3, 2, 1)
    assert result["delta"]["review"] == 0


def test_tightening_a_threshold_reports_a_negative_delta():
    """A what-if is not only a relaxation — a sponsor asking for a stricter bound
    needs the cost of it, and a panel that only ever counted up would hide it."""
    result = simulation.simulate(VALUES, [_override(EGFR_KEY, 75)])
    assert _buckets(result["simulated"]) == (0, 2, 4)
    assert result["delta"] == {"eligible": -1, "review": 0, "ineligible": 1}


def test_relaxing_an_exclusion_bound_stops_it_ruling_a_patient_out():
    """The exclusion side is flipped, so a *higher* bound is the relaxation."""
    result = simulation.simulate(VALUES, [_override(SBP_KEY, 180)])
    assert _buckets(result["simulated"]) == (2, 2, 2)
    assert result["delta"]["eligible"] == 1


def test_tightening_an_exclusion_bound_rules_more_patients_out():
    result = simulation.simulate(VALUES, [_override(SBP_KEY, 110)])
    # Everyone's systolic BP is at or above 110, so the exclusion now catches the
    # whole cohort — including the patient who was the only match.
    assert _buckets(result["simulated"]) == (0, 2, 4)


def test_several_thresholds_move_together():
    """Simulating one at a time would hide the interaction: PT-6 needs the
    exclusion moved *and* nothing else, PT-2 needs the inclusion moved."""
    result = simulation.simulate(VALUES, [_override(EGFR_KEY, 50), _override(SBP_KEY, 180)])
    assert _buckets(result["simulated"]) == (3, 2, 1)


def test_a_between_bound_uses_both_of_its_ends():
    result = simulation.simulate(VALUES, [_override(EGFR_KEY, 50, operator="between", high=60)])
    # Only PT-2 (55) is inside the window; PT-1's 70 now fails the upper end.
    assert _buckets(result["simulated"]) == (1, 2, 3)
    assert result["overrides"][0]["after"] == "egfr between 50–60 mL/min/1.73m2"


def test_the_simulated_attrition_is_recomputed_not_echoed():
    """The panel's whole second act: once eGFR stops binding, which criterion does?"""
    result = simulation.simulate(VALUES, [_override(EGFR_KEY, 40)])
    moved = result["overrides"][0]["simulated_key"]
    assert _rows(result["current"])[EGFR_KEY]["excluded"] == 3
    assert _rows(result["simulated"])[moved]["excluded"] == 0
    # And the criterion the relaxation exposes is now the most restrictive one.
    assert result["simulated"]["criteria"][0]["key"] == SBP_KEY


def test_a_moved_criterion_is_relabelled_on_the_simulated_side():
    """Its identity *is* its label, so the simulated row is not the current row's
    key — and the override says which row it became rather than leaving the panel
    to re-derive a rule that lives in `services/attrition.py`."""
    result = simulation.simulate(VALUES, [_override(EGFR_KEY, 40)])
    echoed = result["overrides"][0]
    assert echoed["key"] == EGFR_KEY
    assert echoed["simulated_key"] == "inclusion:egfr >= 40 mL/min/1.73m2"
    assert echoed["simulated_key"] in _rows(result["simulated"])
    assert echoed["key"] not in _rows(result["simulated"])


def test_criteria_nobody_overrode_keep_their_verdicts_exactly():
    """Especially the categorical one — re-deciding it is the LLM pass this
    feature exists not to make."""
    result = simulation.simulate(VALUES, [_override(EGFR_KEY, 40)])
    for key in (AGE_KEY, PLATELETS_KEY, NSCLC_KEY):
        before, after = _rows(result["current"])[key], _rows(result["simulated"])[key]
        assert (before["excluded"], before["unresolved"], before["passed"]) == (
            after["excluded"],
            after["unresolved"],
            after["passed"],
        ), key


def test_a_patient_with_no_value_on_file_stays_undecidable_at_every_threshold():
    for value in (10, 50, 200):
        result = simulation.simulate(VALUES, [_override(EGFR_KEY, value)])
        moved = result["overrides"][0]["simulated_key"]
        assert _rows(result["simulated"])[moved]["unresolved"] == 1
        # Their record was read, so nothing about them was left un-simulated.
        assert result["overrides"][0]["unavailable"] == 0


def test_the_simulated_buckets_come_from_the_cohort_module():
    """Not a second implementation of "who is eligible" — the same one."""
    result = simulation.simulate(VALUES, [_override(EGFR_KEY, 50)])
    simulated = result["simulated"]["totals"]
    assert (simulated["eligible"], simulated["review"], simulated["ineligible"]) == (2, 2, 2)
    assert simulated["patients"] == len(COHORT)


# --- Runs scored before the value was recorded -------------------------------


def _legacy(cohort_rows: list[dict]) -> list[dict]:
    """The same cohort as a pre-#95 build wrote it: no `observed` anywhere."""
    return [
        {
            **evaluation,
            "criterion_results": [
                {k: v for k, v in result.items() if k != "observed"}
                for result in evaluation["criterion_results"]
            ],
        }
        for evaluation in cohort_rows
    ]


def test_a_run_scored_before_the_value_was_recorded_reports_what_it_could_not_check():
    """Holding those patients at their old status while reporting a delta would
    understate the change; the count is what lets a reader discount it."""
    values = {**VALUES, "matched_patients": _legacy(COHORT)}
    result = simulation.simulate(values, [_override(EGFR_KEY, 40)])

    assert result["overrides"][0]["unavailable"] == len(COHORT)
    # Nothing moved, because nothing could be re-checked.
    assert result["delta"] == {"eligible": 0, "review": 0, "ineligible": 0}
    assert _buckets(result["simulated"]) == _buckets(result["current"])


def test_unavailable_counts_patients_not_rows():
    """A criterion the protocol quotes twice is one row on the panel, so reporting
    two un-recheckable patients for one patient would read as a cohort twice the
    size of the one being simulated."""
    doubled = {**EGFR, "source_text": "Renal function: eGFR >= 60."}
    values = {
        **VALUES,
        "parsed_criteria": {**CRITERIA, "inclusion_quantitative": [EGFR, doubled]},
        "matched_patients": [
            {
                "patient_id": "PT-1",
                "eligible": False,
                "needs_review": False,
                "criterion_results": [
                    {"criterion": EGFR, "kind": "inclusion", "status": "fail"},
                    {"criterion": doubled, "kind": "inclusion", "status": "fail"},
                ],
            }
        ],
    }
    result = simulation.simulate(values, [_override(EGFR_KEY, 30)])
    assert result["overrides"][0]["unavailable"] == 1


def test_the_simulated_breakdown_reports_the_same_value_span_the_current_one_does():
    """Dropping `observed` from the re-scored rows would leave every simulated
    threshold with empty slider bounds — a trap for the next caller."""
    result = simulation.simulate(VALUES, [_override(EGFR_KEY, 50)])
    moved = result["overrides"][0]["simulated_key"]
    before = _rows(result["current"])[EGFR_KEY]["threshold"]
    after = _rows(result["simulated"])[moved]["threshold"]
    assert before is not None and after is not None
    assert (after["observed_min"], after["observed_max"]) == (
        before["observed_min"],
        before["observed_max"],
    )
    assert after["value"] == 50


def test_a_partially_recorded_cohort_re_checks_only_what_it_can():
    values = {**VALUES, "matched_patients": [*_legacy(COHORT[:2]), *COHORT[2:]]}
    result = simulation.simulate(values, [_override(EGFR_KEY, 40)])

    assert result["overrides"][0]["unavailable"] == 2
    # PT-2 is one of the two that could not be re-checked, so only PT-3 and PT-4
    # move — and PT-4 only as far as review.
    assert result["delta"] == {"eligible": 1, "review": 0, "ineligible": -1}


# --- Overrides that cannot be honored ----------------------------------------


def test_a_categorical_criterion_cannot_be_simulated():
    with pytest.raises(InvalidSimulationError) as excinfo:
        simulation.simulate(VALUES, [_override(NSCLC_KEY, 1)])
    assert "categorical" in str(excinfo.value)


def test_an_unknown_criterion_is_refused_rather_than_ignored():
    """Skipping it would report the unchanged cohort as the simulated one."""
    with pytest.raises(InvalidSimulationError):
        simulation.simulate(VALUES, [_override("inclusion:egfr >= 99 mL/min/1.73m2", 50)])


def test_one_criterion_cannot_carry_two_thresholds():
    with pytest.raises(InvalidSimulationError):
        simulation.simulate(VALUES, [_override(EGFR_KEY, 50), _override(EGFR_KEY, 40)])


def test_a_between_override_needs_its_upper_bound():
    with pytest.raises(InvalidSimulationError):
        simulation.simulate(VALUES, [_override(EGFR_KEY, 50, operator="between")])


def test_a_between_override_with_its_bounds_reversed_is_refused():
    """`60 <= v <= 30` holds for nobody, so simulating it would answer "no patient
    is eligible" with a straight face — and carry the inverted window into the
    promotable payload, where the Critic's range check (lower bound only) would
    not catch it either."""
    with pytest.raises(InvalidSimulationError) as excinfo:
        simulation.simulate(VALUES, [_override(EGFR_KEY, 60, operator="between", high=30)])
    assert "wrong way round" in str(excinfo.value)
    # The degenerate single-point window is legal — it is a real, if narrow, ask.
    simulation.simulate(VALUES, [_override(EGFR_KEY, 55, operator="between", high=55)])


def test_a_criterion_with_no_attribute_to_compare_is_refused_not_crashed_on():
    """A hand-edited checkpoint, but every quantitative path downstream reads
    `attribute` unconditionally — the rule engine's range check included — so this
    has to be a 422 rather than a 500 on a read-only endpoint."""
    headless = {k: v for k, v in EGFR.items() if k != "attribute"}
    values = {
        **VALUES,
        "parsed_criteria": {**CRITERIA, "inclusion_quantitative": [headless]},
        "matched_patients": [
            {
                "patient_id": "PT-1",
                "eligible": False,
                "needs_review": False,
                "criterion_results": [
                    {"criterion": headless, "kind": "inclusion", "status": "fail", "observed": 40}
                ],
            }
        ],
    }
    key = f"inclusion:{headless['value']} ()"
    with pytest.raises(InvalidSimulationError) as excinfo:
        simulation.simulate(values, [_override(key, 30)])
    assert "no patient attribute" in str(excinfo.value)


def test_a_non_numeric_observed_value_degrades_instead_of_raising():
    """`compare_quantitative` would raise TypeError on a string; the checkpoint is
    not a place this module gets to assume types from."""
    values = {
        **VALUES,
        "matched_patients": [
            {
                "patient_id": "PT-1",
                "eligible": False,
                "needs_review": False,
                "criterion_results": [
                    {"criterion": EGFR, "kind": "inclusion", "status": "fail", "observed": "42ish"}
                ],
            }
        ],
    }
    result = simulation.simulate(values, [_override(EGFR_KEY, 30)])
    # Unreadable is undecidable: the patient needs a human, not a silent pass.
    assert _buckets(result["simulated"]) == (0, 1, 0)


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"parsed_criteria": CRITERIA},
        {"matched_patients": COHORT},
        {"parsed_criteria": CRITERIA, "matched_patients": []},
    ],
)
def test_a_run_with_no_scored_cohort_cannot_be_simulated(values):
    with pytest.raises(ScreeningNotSimulatableError):
        simulation.simulate(values, [_override(EGFR_KEY, 50)])


# --- The Critic's verdict on the simulated value ------------------------------


def test_an_implausible_simulated_threshold_is_flagged_by_the_rule_that_would_block_it():
    """PLT-001, the units slip that rules out every patient — caught here rather
    than after a reviewer has talked themselves into promoting it."""
    result = simulation.simulate(VALUES, [_override(PLATELETS_KEY, 100_000)])
    findings = result["overrides"][0]["findings"]
    assert [finding["rule_id"] for finding in findings] == ["PLT-001"]
    assert findings[0]["severity"] == "reject"
    # And the plain-language layer (#52) survives the trip.
    assert "units slip" in findings[0]["explanation"]


def test_a_plausible_simulated_threshold_is_not_flagged():
    result = simulation.simulate(VALUES, [_override(PLATELETS_KEY, 150)])
    assert result["overrides"][0]["findings"] == []


def test_the_whole_extraction_is_not_re_audited_only_the_moved_threshold():
    """Running the full rule set over one criterion in isolation would report a
    missing age bound for every simulation of every protocol."""
    result = simulation.simulate(VALUES, [_override(EGFR_KEY, 50)])
    assert result["overrides"][0]["findings"] == []


def test_an_override_echoes_the_criterion_both_ways_round():
    result = simulation.simulate(VALUES, [_override(EGFR_KEY, 50)])
    echoed = result["overrides"][0]
    assert echoed["key"] == EGFR_KEY
    assert echoed["kind"] == "inclusion"
    assert echoed["attribute"] == "egfr"
    assert echoed["unit"] == "mL/min/1.73m2"
    assert echoed["before"] == "egfr >= 60 mL/min/1.73m2"
    assert echoed["after"] == "egfr >= 50 mL/min/1.73m2"


# --- The promotion payload ----------------------------------------------------


def test_the_promotable_criteria_carry_the_override_and_nothing_else():
    result = simulation.simulate(VALUES, [_override(EGFR_KEY, 50)])
    promoted = result["criteria"]

    egfr = promoted["inclusion_quantitative"][1]
    assert (egfr["value"], egfr["operator"]) == (50, ">=")
    # Provenance is untouched: the protocol still says what it said, and an edit
    # that rewrote the sentence would make any threshold look justified.
    assert egfr["source_text"] == EGFR["source_text"]
    assert promoted["inclusion_quantitative"][0] == AGE
    assert promoted["inclusion_categorical"] == [NSCLC]
    assert promoted["exclusion_quantitative"] == [SBP]
    assert promoted["trial_title"] == "A trial"
    # And the run's own criteria are not mutated by having been simulated.
    assert EGFR["value"] == 60.0


def test_moving_off_between_clears_the_upper_bound_it_leaves_behind():
    windowed = {**EGFR, "operator": "between", "value": 30.0, "value_high": 60.0}
    values = {
        **VALUES,
        "parsed_criteria": {**CRITERIA, "inclusion_quantitative": [windowed]},
        "matched_patients": [
            matcher.evaluate_patient(
                patient, {**CRITERIA, "inclusion_quantitative": [windowed]}, VERDICTS
            )
            for patient in PATIENTS
        ],
    }
    key = "inclusion:egfr between 30–60 mL/min/1.73m2"
    result = simulation.simulate(values, [_override(key, 50)])
    assert result["criteria"]["inclusion_quantitative"][0]["value_high"] is None


def test_the_promotion_payload_carries_the_revision_it_was_built_against():
    """So a promote is one PATCH derived from one response, not two reads."""
    assert simulation.simulate(VALUES, [_override(EGFR_KEY, 50)])["criteria_revision"] == 2


# --- The route ---------------------------------------------------------------


PROTOCOL = (
    "Phase II single-arm study of an investigational agent in adults.\n\n"
    "Inclusion criteria:\n"
    "- Age 18 years or older at the time of consent.\n\n"
    "Exclusion criteria:\n"
    "- Uncontrolled hypertension.\n"
)


class FakeSnapshot:
    def __init__(self, values: dict[str, Any], pending: tuple = ()):
        self.values = values
        self.next = pending


class FakeGraph:
    """One fixed snapshot, and an `aupdate_state` that fails the test if reached.

    The refusal is the point: "without mutating the checkpoint" is the guarantee
    this endpoint is sold on, and a fake that quietly accepted a write would let it
    be broken silently.
    """

    def __init__(self, snapshot: FakeSnapshot):
        self.snapshot = snapshot

    async def aget_state(self, _config: object) -> FakeSnapshot:
        return self.snapshot

    async def astream(
        self, input: object, config: object = None, *, stream_mode: object = None
    ) -> AsyncIterator[dict]:
        raise AssertionError("simulating must not run the graph")
        yield {}

    async def ainvoke(self, *_a: object, **_k: object) -> dict:
        raise AssertionError("simulating must not run the graph")

    async def aupdate_state(
        self, _config: object, _values: dict, as_node: str | None = None
    ) -> None:
        raise AssertionError("simulating must not write to the checkpoint")


@pytest.fixture
def client():
    with TestClient(main.app, raise_server_exceptions=False) as c:
        sign_in(c)
        yield c


@pytest.fixture
def no_llm(monkeypatch):
    """Every door to the model, bolted shut for the duration of the test."""

    def forbidden():
        raise AssertionError("a simulation must not call the LLM")

    monkeypatch.setattr(matcher_mod, "get_llm", forbidden)
    monkeypatch.setattr(critic_mod, "get_llm", forbidden)


def _create(client) -> str:
    upload = client.post(
        "/api/screenings", files={"file": ("protocol.md", PROTOCOL.encode(), "text/markdown")}
    )
    assert upload.status_code == 200
    return str(upload.json()["thread_id"])


def _simulate(client, thread_id: str, overrides: list[dict]):
    return client.post(f"/api/screenings/{thread_id}/simulate", json={"overrides": overrides})


def test_simulate_serves_both_sides_and_the_delta(client, monkeypatch, no_llm):
    thread_id = _create(client)
    monkeypatch.setattr(main, "graph", FakeGraph(FakeSnapshot(VALUES)))

    response = _simulate(client, thread_id, [{"key": EGFR_KEY, "operator": ">=", "value": 50}])

    assert response.status_code == 200
    body = response.json()
    assert body["current"]["totals"]["eligible"] == 1
    assert body["simulated"]["totals"]["eligible"] == 2
    assert body["delta"] == {"eligible": 1, "review": 0, "ineligible": -1}
    assert body["overrides"][0]["after"] == "egfr >= 50 mL/min/1.73m2"


def test_simulating_makes_no_llm_call_and_writes_nothing(client, monkeypatch, no_llm):
    """AC 2, and the reason the feature is worth having at all.

    `FakeGraph.aupdate_state` asserts on the write; `no_llm` asserts on the model.
    Both are wired up here rather than described in a comment because "it's free"
    is the entire premise — a simulation that quietly cost a cohort's worth of
    term mappings would be slower than the re-run it replaces.
    """
    thread_id = _create(client)
    graph = FakeGraph(FakeSnapshot(VALUES))
    monkeypatch.setattr(main, "graph", graph)

    for value in (40, 50, 60, 70, 80):
        assert (
            _simulate(
                client, thread_id, [{"key": EGFR_KEY, "operator": ">=", "value": value}]
            ).status_code
            == 200
        )
    # The checkpoint is the object it was handed, verbatim.
    assert graph.snapshot.values["matched_patients"] is COHORT


def test_simulate_on_a_run_with_no_cohort_is_conflict(client, monkeypatch):
    thread_id = _create(client)
    monkeypatch.setattr(main, "graph", FakeGraph(FakeSnapshot({"parsed_criteria": CRITERIA})))

    response = _simulate(client, thread_id, [{"key": EGFR_KEY, "operator": ">=", "value": 50}])

    assert response.status_code == 409
    assert response.json()["error"] == "ScreeningNotSimulatableError"


def test_simulate_on_an_unknown_criterion_is_unprocessable(client, monkeypatch):
    thread_id = _create(client)
    monkeypatch.setattr(main, "graph", FakeGraph(FakeSnapshot(VALUES)))

    response = _simulate(
        client, thread_id, [{"key": "inclusion:nope", "operator": ">=", "value": 1}]
    )

    assert response.status_code == 422
    assert response.json()["error"] == "InvalidSimulationError"


def test_simulate_on_an_unknown_thread_is_404(client):
    assert (
        _simulate(client, "nope", [{"key": EGFR_KEY, "operator": ">=", "value": 50}]).status_code
        == 404
    )


def test_simulate_needs_at_least_one_override(client, monkeypatch):
    """An empty what-if is a request for the cohort the caller already has."""
    thread_id = _create(client)
    monkeypatch.setattr(main, "graph", FakeGraph(FakeSnapshot(VALUES)))
    assert _simulate(client, thread_id, []).status_code == 422


def test_simulate_refuses_more_overrides_than_a_protocol_could_have(client, monkeypatch):
    thread_id = _create(client)
    monkeypatch.setattr(main, "graph", FakeGraph(FakeSnapshot(VALUES)))
    too_many = [
        {"key": EGFR_KEY, "operator": ">=", "value": float(n)}
        for n in range(main.MAX_SIMULATION_OVERRIDES + 1)
    ]
    assert _simulate(client, thread_id, too_many).status_code == 422


def test_simulate_and_state_agree_about_the_run_as_it_stands(client, monkeypatch, no_llm):
    """`current` is not a second reading of the checkpoint — the reviewer is
    comparing the simulated column against the panel already on their screen, and
    two derivations of "as it stands" would eventually put a delta between them."""
    thread_id = _create(client)
    monkeypatch.setattr(main, "graph", FakeGraph(FakeSnapshot(VALUES)))

    state = client.get(f"/api/screenings/{thread_id}/state").json()
    simulated = _simulate(
        client, thread_id, [{"key": EGFR_KEY, "operator": ">=", "value": 50}]
    ).json()

    assert simulated["current"] == state["attrition"]


# --- Promoting a what-if into a real edit (AC 5) ------------------------------


async def test_a_simulated_threshold_can_be_promoted_through_the_existing_edit_path(monkeypatch):
    """End to end, through the real graph: screen a cohort, simulate a tighter age
    bound, then promote the response's own `criteria` payload.

    Promotion is deliberately not its own write path — it is `PATCH /criteria`
    with the payload the simulation handed back, so a promoted threshold gets the
    same revision check, the same Critic re-run and the same audit entry as any
    hand-typed correction. A what-if that reached the criteria without passing the
    Critic would be exactly the hole the gate exists to close.
    """
    monkeypatch.setattr(parser_mod, "get_llm", lambda: FakeChatModel([good_criteria()]))
    monkeypatch.setattr(critic_mod, "run_llm_semantic_review", lambda _state: [])
    monkeypatch.setattr(matcher_mod, "load_patients", lambda: FAKE_PATIENTS)

    async with main.lifespan(main.app):
        transport = ASGITransport(app=main.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            sign_in(client)
            upload = await client.post(
                "/api/screenings",
                files={"file": ("protocol.md", PROTOCOL_TEXT.encode(), "text/markdown")},
            )
            thread_id = upload.json()["thread_id"]
            async with client.stream("GET", f"/api/screenings/{thread_id}/stream") as resp:
                assert [line async for line in resp.aiter_lines()]
            async with client.stream("POST", f"/api/screenings/{thread_id}/approve") as approve:
                assert [line async for line in approve.aiter_lines()]

            # Three patients aged 30, 52 and 71 all clear "age >= 18".
            state = (await client.get(f"/api/screenings/{thread_id}/state")).json()
            assert state["attrition"]["totals"]["eligible"] == 3
            age_key = state["attrition"]["criteria"][0]["key"]
            assert age_key == "inclusion:age >= 18 years"

            simulation_response = await client.post(
                f"/api/screenings/{thread_id}/simulate",
                json={"overrides": [{"key": age_key, "operator": ">=", "value": 60}]},
            )
            assert simulation_response.status_code == 200
            what_if = simulation_response.json()
            assert what_if["delta"] == {"eligible": -2, "review": 0, "ineligible": 2}
            # Still the run it was: simulating changed nothing about it.
            unchanged = (await client.get(f"/api/screenings/{thread_id}/state")).json()
            assert unchanged["attrition"] == state["attrition"]
            assert unchanged["values"]["criteria_revision"] == what_if["criteria_revision"]

            async with client.stream(
                "PATCH",
                f"/api/screenings/{thread_id}/criteria",
                json={
                    "base_revision": what_if["criteria_revision"],
                    "criteria": what_if["criteria"],
                },
            ) as promoted:
                assert promoted.status_code == 200
                assert [line async for line in promoted.aiter_lines()]

            after = (await client.get(f"/api/screenings/{thread_id}/state")).json()
            # The threshold the reviewer simulated is now the run's own.
            assert after["values"]["parsed_criteria"]["inclusion_quantitative"][0]["value"] == 60
            assert after["values"]["criteria_revision"] == 1
            # Through the Critic and back to the gate — no cohort until a named
            # reviewer approves this extraction too.
            assert after["pending"] == ["matcher"]
            assert after["values"]["matched_patients"] == []
            # And the edit is on the record as a revision, not as a mystery.
            assert after["values"]["criteria_edits"][0]["changes"] == [
                {
                    "bucket": "inclusion_quantitative",
                    "from_bucket": None,
                    "kind": "modified",
                    "before": "age >= 18 years",
                    "after": "age >= 60 years",
                }
            ]


def test_simulate_refuses_a_threshold_that_is_not_a_real_number(client, monkeypatch):
    """Python's JSON parser accepts `NaN`; a NaN bound compares false against every
    value, so it would answer "nobody is eligible" with a straight face."""
    thread_id = _create(client)
    monkeypatch.setattr(main, "graph", FakeGraph(FakeSnapshot(VALUES)))
    response = client.post(
        f"/api/screenings/{thread_id}/simulate",
        content=f'{{"overrides": [{{"key": "{EGFR_KEY}", "operator": ">=", "value": NaN}}]}}',
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
