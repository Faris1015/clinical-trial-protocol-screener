"""Comparing two runs side by side (#59) — the reduction and the route behind it.

Two halves. `app/services/comparison.py` pairs two `get_screening_state` payloads
into one side-by-side view: the criteria matched up by provenance and typed
(unchanged / modified / added / removed), and the cohort's verdicts matched up by
patient. `GET /api/screenings/compare?a=…&b=…` serves it.

The tests that matter most are the *agreement* ones. A comparison is only worth
anything if each of its columns says what that run's own page says — a view
claiming a criterion changed when neither run's detail page shows it changed would
be worse than no view, because a coordinator would re-parse a protocol on the
strength of it. So the header counts are checked against the same run's `/state`
payload, and the eligible tally against the figure the runs index denormalizes.
"""

from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.services import cohort, comparison
from tests.auth_helpers import sign_in

# --- payload builders --------------------------------------------------------

AGE_SENTENCE = "Age 18 years or older at the time of consent."
ORGAN_SENTENCE = "Adequate bone marrow and organ function per investigator assessment."
NSCLC_SENTENCE = "Histologically confirmed non-small cell lung cancer."


def _quant(value: float = 18, *, attribute: str = "age", source: str = AGE_SENTENCE) -> dict:
    return {
        "attribute": attribute,
        "operator": ">=",
        "value": value,
        "value_high": None,
        "unit": "years",
        "source_text": source,
    }


def _categorical(value: str = "NSCLC", *, source: str = NSCLC_SENTENCE) -> dict:
    return {"category": "diagnosis", "value": value, "negated": False, "source_text": source}


def _criteria(**overrides) -> dict:
    base: dict = {
        "trial_title": "A Trial",
        "inclusion_quantitative": [],
        "inclusion_categorical": [],
        "exclusion_quantitative": [],
        "exclusion_categorical": [],
        "unparseable": [],
    }
    base.update(overrides)
    return base


def _patient(patient_id: str, *, eligible: bool = True, needs_review: bool = False) -> dict:
    return {
        "patient_id": patient_id,
        "name": f"Patient {patient_id}",
        "eligible": eligible,
        "needs_review": needs_review,
        # Carried on a real evaluation and deliberately *not* echoed by a
        # comparison; a test below asserts it stays off the wire.
        "criterion_results": [{"criterion": _quant(), "kind": "inclusion", "status": "pass"}],
    }


def _payload(
    *,
    values: dict | None = None,
    pending: list[str] | None = None,
    thread_id: str = "run-a",
    filename: str = "protocol.pdf",
    status: str = "done",
    created_at: str = "2026-08-01T09:00:00+00:00",
) -> dict:
    """One `get_screening_state` response — the input a comparison is built from."""
    return {
        "values": values if values is not None else {},
        "pending": pending or [],
        "screening": {
            "thread_id": thread_id,
            "source_filename": filename,
            "status": status,
            "created_at": created_at,
            "criteria_count": 0,
            "match_count": 0,
        },
    }


def _rows(result: dict, bucket: str) -> list[dict]:
    """The compared rows of one criteria bucket, or [] when it was omitted."""
    for entry in result["criteria"]["buckets"]:
        if entry["bucket"] == bucket:
            return list(entry["rows"])
    return []


def _kinds(result: dict, bucket: str) -> list[str]:
    return [row["kind"] for row in _rows(result, bucket)]


def _compare(a_values: dict, b_values: dict, **kwargs) -> dict:
    return comparison.compare_runs(
        _payload(values=a_values, thread_id="run-a"),
        _payload(values=b_values, thread_id="run-b", **kwargs),
    )


# --- criteria: the same extraction twice ------------------------------------


def test_two_identical_extractions_compare_as_identical():
    """The re-parse case the issue opens with: same protocol, same result."""
    criteria = _criteria(inclusion_quantitative=[_quant()], inclusion_categorical=[_categorical()])
    result = _compare({"parsed_criteria": criteria}, {"parsed_criteria": dict(criteria)})

    assert result["criteria"]["identical"] is True
    assert result["criteria"]["differences"] == 0
    assert result["criteria"]["totals"]["unchanged"] == 2
    assert _kinds(result, "inclusion_quantitative") == ["unchanged"]
    # Both sides are still carried: a side-by-side that dropped the agreeing rows
    # would leave the reader unable to see what the runs agreed on.
    row = _rows(result, "inclusion_quantitative")[0]
    assert row["a"] == row["b"] == "age >= 18 years"


def test_criteria_are_paired_by_provenance_not_by_position():
    """A re-parse that emitted the same criteria in a different order changed nothing.

    An index-wise pairing would report both rows as modified here, which is the
    failure mode that makes a diff useless: every re-run would look like a rewrite.
    """
    age = _quant()
    bmi = _quant(attribute="bmi", source="Body mass index at or above 18.5.")

    result = _compare(
        {"parsed_criteria": _criteria(inclusion_quantitative=[age, bmi])},
        {"parsed_criteria": _criteria(inclusion_quantitative=[bmi, age])},
    )

    assert _kinds(result, "inclusion_quantitative") == ["unchanged", "unchanged"]
    assert result["criteria"]["identical"] is True


def test_two_criteria_quoting_one_sentence_pair_up_among_themselves():
    """The duplicate-provenance case: same key twice, so the pairing falls back to
    position *within* that key rather than matching both against the first one."""
    left = _quant(18)
    right = _quant(65)

    result = _compare(
        {"parsed_criteria": _criteria(inclusion_quantitative=[left, right])},
        {"parsed_criteria": _criteria(inclusion_quantitative=[left, right])},
    )

    assert _kinds(result, "inclusion_quantitative") == ["unchanged", "unchanged"]


# --- criteria: the three differences the issue asks to be highlighted -------


def test_a_changed_threshold_on_the_same_sentence_is_one_modified_row():
    result = _compare(
        {"parsed_criteria": _criteria(inclusion_quantitative=[_quant(18)])},
        {"parsed_criteria": _criteria(inclusion_quantitative=[_quant(65)])},
    )

    (row,) = _rows(result, "inclusion_quantitative")
    assert row == {"kind": "modified", "a": "age >= 18 years", "b": "age >= 65 years"}
    assert result["criteria"]["identical"] is False
    assert result["criteria"]["differences"] == 1


def test_a_criterion_only_the_second_run_found_is_added():
    result = _compare(
        {"parsed_criteria": _criteria()},
        {"parsed_criteria": _criteria(inclusion_categorical=[_categorical()])},
    )

    (row,) = _rows(result, "inclusion_categorical")
    assert row["kind"] == "added"
    assert row["a"] is None
    assert row["b"] == "NSCLC (diagnosis)"
    assert result["criteria"]["totals"]["added"] == 1


def test_a_criterion_only_the_first_run_found_is_removed():
    result = _compare(
        {"parsed_criteria": _criteria(exclusion_quantitative=[_quant(attribute="egfr")])},
        {"parsed_criteria": _criteria()},
    )

    (row,) = _rows(result, "exclusion_quantitative")
    assert row["kind"] == "removed"
    assert row["b"] is None
    assert result["criteria"]["totals"]["removed"] == 1


def test_added_and_removed_are_stated_from_the_first_runs_point_of_view():
    """`a`/`b` follow the query parameters, so swapping the pair mirrors the diff."""
    only_a = {"parsed_criteria": _criteria(inclusion_quantitative=[_quant()])}
    empty = {"parsed_criteria": _criteria()}

    assert _kinds(_compare(only_a, empty), "inclusion_quantitative") == ["removed"]
    assert _kinds(_compare(empty, only_a), "inclusion_quantitative") == ["added"]


def test_the_rows_read_down_the_first_runs_own_order_with_the_seconds_extras_last():
    """A's extraction reads down the left column in its own order; B's extras follow."""
    result = _compare(
        {
            "parsed_criteria": _criteria(
                inclusion_quantitative=[
                    _quant(attribute="egfr", source="Renal function."),
                    _quant(source=AGE_SENTENCE),
                ]
            )
        },
        {
            "parsed_criteria": _criteria(
                inclusion_quantitative=[
                    _quant(source=AGE_SENTENCE),
                    _quant(attribute="bmi", source="Body mass."),
                ]
            )
        },
    )

    assert _kinds(result, "inclusion_quantitative") == ["removed", "unchanged", "added"]
    assert [row["a"] for row in _rows(result, "inclusion_quantitative")] == [
        "egfr >= 18 years",
        "age >= 18 years",
        None,
    ]


def test_the_same_criterion_quoted_from_different_sentences_is_not_a_difference():
    """Two *different* protocols is half of what this view is for, and each quotes its
    own eligibility section — so provenance cannot be the only pairing key. The
    criterion is identical; only the sentence behind it differs."""
    result = _compare(
        {"parsed_criteria": _criteria(inclusion_quantitative=[_quant(source="Adults 18+.")])},
        {
            "parsed_criteria": _criteria(
                inclusion_quantitative=[_quant(source="Participants must be at least 18.")]
            )
        },
    )

    assert _kinds(result, "inclusion_quantitative") == ["unchanged"]
    assert result["criteria"]["identical"] is True


def test_a_different_threshold_quoted_from_a_different_sentence_is_still_two_rows():
    """The label fallback pairs identical criteria, never merely similar ones: two
    trials asking for different ages are two requirements, not one modified row."""
    result = _compare(
        {"parsed_criteria": _criteria(inclusion_quantitative=[_quant(18, source="Adults 18+.")])},
        {"parsed_criteria": _criteria(inclusion_quantitative=[_quant(65, source="Aged 65+.")])},
    )

    assert _kinds(result, "inclusion_quantitative") == ["removed", "added"]
    assert result["criteria"]["differences"] == 2


def test_a_sentence_one_run_gave_up_on_shows_on_both_sides():
    """The bucket-scoped pairing, stated: `unparseable` in A, a real criterion in B.

    Two independent extractions, so this is deliberately *not* folded into one
    `reclassified` row the way a reviewer's own edit would be (services/
    criteria_edits.py) — a reviewer has to see that one run failed to read the
    sentence and the other read it as an inclusion criterion.
    """
    result = _compare(
        {"parsed_criteria": _criteria(unparseable=[ORGAN_SENTENCE])},
        {"parsed_criteria": _criteria(inclusion_quantitative=[_quant(source=ORGAN_SENTENCE)])},
    )

    assert _kinds(result, "unparseable") == ["removed"]
    assert _kinds(result, "inclusion_quantitative") == ["added"]
    assert result["criteria"]["differences"] == 2


def test_buckets_neither_run_used_are_omitted():
    result = _compare(
        {"parsed_criteria": _criteria(inclusion_quantitative=[_quant()])},
        {"parsed_criteria": _criteria(inclusion_quantitative=[_quant()])},
    )

    assert [entry["bucket"] for entry in result["criteria"]["buckets"]] == [
        "inclusion_quantitative"
    ]


def test_two_runs_that_never_parsed_are_not_called_identical():
    """Two absent extractions are not an agreement — there was nothing to compare."""
    result = _compare({}, {})

    assert result["criteria"]["identical"] is False
    assert result["criteria"]["buckets"] == []
    assert result["criteria"]["differences"] == 0


# --- the run headers ---------------------------------------------------------


def test_each_run_block_carries_what_the_column_is_read_under():
    result = comparison.compare_runs(
        _payload(
            values={
                "parsed_criteria": _criteria(
                    trial_title="NSCLC-2026", inclusion_quantitative=[_quant()]
                ),
                "criteria_revision": 2,
                "matched_patients": [_patient("PT-1"), _patient("PT-2", eligible=False)],
            },
            thread_id="first",
            filename="v1.pdf",
        ),
        _payload(values={}, thread_id="second", filename="v2.pdf", status="routing"),
    )

    first, second = result["runs"]
    assert (first["side"], second["side"]) == ("a", "b")
    assert first["thread_id"] == "first"
    assert first["source_filename"] == "v1.pdf"
    assert first["trial_title"] == "NSCLC-2026"
    assert first["criteria_revision"] == 2
    assert first["criteria_count"] == 1
    assert first["cohort"] == {"eligible": 1, "review": 0, "ineligible": 1, "total": 2}
    assert first["parsed"] is True
    assert first["matched"] is True
    # The run that was uploaded but never streamed: an empty column that has to
    # read as "never ran", not as "the Parser found nothing".
    assert second["status"] == "routing"
    assert second["parsed"] is False
    assert second["matched"] is False
    assert second["criteria_count"] == 0


def test_a_run_parked_at_the_gate_reports_the_gate_not_its_last_step():
    """Same derivation the detail view and the report use: `pending` wins."""
    result = comparison.compare_runs(
        _payload(values={"current_step": "matching"}, pending=["matcher"], status="matching"),
        _payload(values={}),
    )

    assert result["runs"][0]["status"] == "awaiting_approval"


def test_the_criteria_count_excludes_the_sentences_nobody_could_parse():
    """The same exclusion the runs index makes, so the header agrees with the row."""
    result = _compare(
        {
            "parsed_criteria": _criteria(
                inclusion_quantitative=[_quant()], unparseable=[ORGAN_SENTENCE, NSCLC_SENTENCE]
            )
        },
        {},
    )

    assert result["runs"][0]["criteria_count"] == 1


def test_a_runs_filename_falls_back_to_the_checkpoint_when_the_row_is_gone():
    payload = _payload(values={"source_filename": "from-checkpoint.pdf"})
    payload["screening"] = None

    result = comparison.compare_runs(payload, _payload(values={}))

    assert result["runs"][0]["source_filename"] == "from-checkpoint.pdf"
    assert result["runs"][0]["thread_id"] == ""


# --- the cohort --------------------------------------------------------------


def test_a_patient_whose_verdict_moved_is_the_row_a_reviewer_wants_first():
    result = _compare(
        {"matched_patients": [_patient("PT-1"), _patient("PT-2")]},
        {"matched_patients": [_patient("PT-1"), _patient("PT-2", eligible=False)]},
    )

    rows = result["matches"]["patients"]
    assert rows[0]["patient_id"] == "PT-2"
    assert rows[0]["kind"] == "changed"
    assert rows[0]["a"] == {"bucket": "eligible", "label": "Eligible"}
    assert rows[0]["b"] == {"bucket": "ineligible", "label": "Ineligible"}
    assert rows[1]["kind"] == "same"
    assert result["matches"]["totals"] == {"changed": 1, "only_a": 0, "only_b": 0, "same": 1}
    assert result["matches"]["differences"] == 1
    assert result["matches"]["compared"] is True


def test_needs_review_outranks_eligible_on_both_sides():
    """A patient the Matcher could not determine is a review, not a match."""
    result = _compare(
        {"matched_patients": [_patient("PT-1")]},
        {"matched_patients": [_patient("PT-1", needs_review=True)]},
    )

    (row,) = result["matches"]["patients"]
    assert row["kind"] == "changed"
    assert row["b"] == {"bucket": "review", "label": "Needs review"}


def test_a_patient_only_one_run_scored_is_reported_as_such():
    result = _compare(
        {"matched_patients": [_patient("PT-1")]},
        {"matched_patients": [_patient("PT-2")]},
    )

    kinds = {row["patient_id"]: row["kind"] for row in result["matches"]["patients"]}
    assert kinds == {"PT-1": "only_a", "PT-2": "only_b"}
    for row in result["matches"]["patients"]:
        # The verdict is absent, not invented, for the run that never saw them.
        assert (row["a"] is None) != (row["b"] is None)
        assert row["name"] == f"Patient {row['patient_id']}"


def test_differences_are_listed_before_the_patients_both_runs_agreed_on():
    result = _compare(
        {"matched_patients": [_patient(f"PT-{i}") for i in (1, 2, 3)]},
        {
            "matched_patients": [
                _patient("PT-1"),
                _patient("PT-2", eligible=False),
                _patient("PT-4"),
            ]
        },
    )

    assert [row["kind"] for row in result["matches"]["patients"]] == [
        "changed",
        "only_a",
        "only_b",
        "same",
    ]
    # Deterministic across two requests for the same pair: patient-id order within
    # each block.
    assert [row["patient_id"] for row in result["matches"]["patients"]] == [
        "PT-2",
        "PT-3",
        "PT-4",
        "PT-1",
    ]


def test_a_run_that_never_reached_the_matcher_leaves_the_cohort_uncompared():
    """One column is empty because that run stopped at the gate, not because nobody
    was eligible — `compared` is what lets the view say which."""
    result = _compare({"matched_patients": [_patient("PT-1")]}, {"parsed_criteria": _criteria()})

    assert result["matches"]["compared"] is False
    assert [row["kind"] for row in result["matches"]["patients"]] == ["only_a"]


def test_the_cohort_rows_carry_verdicts_only_not_two_full_evaluations():
    """Payload weight: a 300-patient pair would otherwise ship 600 evaluations for a
    view that renders none of their per-criterion detail."""
    result = _compare(
        {"matched_patients": [_patient("PT-1")]}, {"matched_patients": [_patient("PT-1")]}
    )

    (row,) = result["matches"]["patients"]
    assert set(row) == {"patient_id", "name", "kind", "a", "b"}


# --- agreement with the rest of the app --------------------------------------


def test_the_eligible_tally_is_the_figure_the_runs_index_shows():
    """One rule for "who was eligible" (services/cohort.py), three renderings."""
    patients = [
        _patient("PT-1"),
        _patient("PT-2", needs_review=True),
        _patient("PT-3", eligible=False),
        _patient("PT-4"),
    ]

    result = _compare({"matched_patients": patients}, {})

    assert result["runs"][0]["cohort"]["eligible"] == cohort.matched_count(patients)


def test_every_bucket_the_edit_diff_walks_is_compared_too():
    """The two views pair criteria the same way, so they must cover the same buckets."""
    from app.services import criteria_edits

    assert comparison.COMPARED_BUCKETS == criteria_edits.DIFFED_BUCKETS


# --- malformed checkpoints ---------------------------------------------------


@pytest.mark.parametrize("junk", [None, "text", 42, []])
def test_a_criteria_field_that_is_not_a_mapping_compares_as_absent(junk):
    result = _compare({"parsed_criteria": junk}, {"parsed_criteria": _criteria()})

    assert result["runs"][0]["parsed"] is False
    assert result["criteria"]["identical"] is False


@pytest.mark.parametrize("junk", [None, "text", 42, {"not": "a list"}])
def test_a_cohort_field_that_is_not_a_list_compares_as_no_cohort(junk):
    result = _compare({"matched_patients": junk}, {"matched_patients": [_patient("PT-1")]})

    assert result["runs"][0]["cohort"]["total"] == 0
    assert [row["kind"] for row in result["matches"]["patients"]] == ["only_b"]


def test_a_repeated_patient_id_within_one_run_is_one_row():
    """A duplicate id is an EHR defect, not two people: the first evaluation wins,
    so the pairing still means "one row per person"."""
    result = _compare(
        {"matched_patients": [_patient("PT-1"), _patient("PT-1", eligible=False)]},
        {"matched_patients": [_patient("PT-1")]},
    )

    (row,) = result["matches"]["patients"]
    assert row["kind"] == "same"
    assert row["a"] == {"bucket": "eligible", "label": "Eligible"}


def test_a_patient_evaluation_missing_its_id_still_pairs_and_does_not_crash():
    """Defensive: an id-less evaluation keys on "" rather than dropping the row."""
    result = _compare({"matched_patients": [{"name": "No id"}]}, {"matched_patients": []})

    (row,) = result["matches"]["patients"]
    assert row["patient_id"] == ""
    assert row["a"] == {"bucket": "ineligible", "label": "Ineligible"}


# --- Route ------------------------------------------------------------------


class FakeSnapshot:
    def __init__(self, values: dict, pending: tuple = ()):
        self.values = values
        self.next = pending


class FakeGraph:
    """Snapshots keyed by thread id — a comparison reads two different threads.

    The other `ScreeningGraph` methods raise: comparing past runs must never
    execute the pipeline, so reaching any of them is a failure, not a fallback.
    """

    def __init__(self, snapshots: dict[str, FakeSnapshot]):
        self.snapshots = snapshots

    async def aget_state(self, config: dict) -> FakeSnapshot:
        thread_id = config["configurable"]["thread_id"]
        return self.snapshots.get(thread_id, FakeSnapshot({}))

    async def astream(  # pragma: no cover - a comparison never drives the graph
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


def _create(client, filename: str) -> str:
    upload = client.post(
        "/api/screenings", files={"file": (filename, b"Inclusion criteria: age >= 18")}
    )
    assert upload.status_code == 200
    return str(upload.json()["thread_id"])


def test_the_route_compares_two_runs(client, monkeypatch):
    first = _create(client, "v1.md")
    second = _create(client, "v2.md")
    monkeypatch.setattr(
        main,
        "graph",
        FakeGraph(
            {
                first: FakeSnapshot(
                    {
                        "parsed_criteria": _criteria(inclusion_quantitative=[_quant(18)]),
                        "matched_patients": [_patient("PT-1")],
                        "current_step": "done",
                    }
                ),
                second: FakeSnapshot(
                    {
                        "parsed_criteria": _criteria(inclusion_quantitative=[_quant(65)]),
                        "matched_patients": [_patient("PT-1", eligible=False)],
                        "current_step": "done",
                    }
                ),
            }
        ),
    )

    response = client.get(f"/api/screenings/compare?a={first}&b={second}")

    assert response.status_code == 200
    body = response.json()
    assert [run["thread_id"] for run in body["runs"]] == [first, second]
    assert [run["source_filename"] for run in body["runs"]] == ["v1.md", "v2.md"]
    assert body["criteria"]["totals"]["modified"] == 1
    assert body["matches"]["totals"]["changed"] == 1


def test_each_column_agrees_with_that_runs_own_state_endpoint(client, monkeypatch):
    """The criterion that matters: a comparison must not contradict either run's page."""
    first = _create(client, "v1.md")
    second = _create(client, "v2.md")
    values = {
        "parsed_criteria": _criteria(
            inclusion_quantitative=[_quant()], inclusion_categorical=[_categorical()]
        ),
        "matched_patients": [_patient("PT-1"), _patient("PT-2", needs_review=True)],
        "current_step": "done",
    }
    monkeypatch.setattr(
        main, "graph", FakeGraph({first: FakeSnapshot(values), second: FakeSnapshot({})})
    )

    compared = client.get(f"/api/screenings/compare?a={first}&b={second}").json()
    state = client.get(f"/api/screenings/{first}/state").json()

    run = compared["runs"][0]
    criteria = state["values"]["parsed_criteria"]
    assert run["criteria_count"] == len(criteria["inclusion_quantitative"]) + len(
        criteria["inclusion_categorical"]
    )
    assert run["trial_title"] == criteria["trial_title"]
    assert run["status"] == state["screening"]["status"]
    assert run["cohort"]["total"] == len(state["values"]["matched_patients"])


def test_comparing_a_run_with_itself_is_refused(client, monkeypatch):
    thread_id = _create(client, "v1.md")
    monkeypatch.setattr(main, "graph", FakeGraph({}))

    response = client.get(f"/api/screenings/compare?a={thread_id}&b={thread_id}")

    assert response.status_code == 422
    assert "two different runs" in response.json()["detail"]


def test_an_unknown_run_is_a_404_naming_the_missing_one(client, monkeypatch):
    thread_id = _create(client, "v1.md")
    monkeypatch.setattr(main, "graph", FakeGraph({}))

    response = client.get(f"/api/screenings/compare?a={thread_id}&b=does-not-exist")

    assert response.status_code == 404
    assert "does-not-exist" in response.json()["detail"]


@pytest.mark.parametrize("query", ["", "?a=only-one", "?b=only-one", "?a=&b=x"])
def test_a_comparison_needs_both_ids(client, query):
    assert client.get(f"/api/screenings/compare{query}").status_code == 422


def test_the_compare_route_is_not_swallowed_by_the_thread_id_routes(client, monkeypatch):
    """`/compare` is a collection route beside `/{thread_id}/…`, not a thread id.

    Regression guard for the one way this endpoint could break silently: if it were
    ever declared as (or shadowed by) a `{thread_id}` path, the request would 404 as
    "no screening found for thread_id compare" instead of comparing anything.
    """
    monkeypatch.setattr(main, "graph", FakeGraph({}))

    response = client.get("/api/screenings/compare?a=nope&b=also-nope")

    assert response.status_code == 404
    assert "compare" not in response.json()["detail"]
