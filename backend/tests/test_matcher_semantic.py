"""Matcher categorical term-mapping (#16).

The deterministic boundaries live in test_matcher; here we cover the two-tier
categorical resolution: the word-boundary fast path (exact match / clear
absence, zero LLM), and the LLM tail for the ambiguous cases — the "small cell"
vs "non-small cell" regression, semantic equivalence like platinum/carboplatin,
"uncertain" routing to needs-review, and the per-screening verdict cache.

The LLM term-mapper is injected as a plain callable so these run offline.
"""

import app.graph.nodes.matcher as matcher_mod
from app.exceptions import LLMUnavailableError
from app.graph.nodes.matcher import (
    build_verdict_cache,
    evaluate_patient,
    fast_present,
)
from app.schemas.review import TermMapping, TermMatch
from tests.fakes import FakeChatModel


def _cat(value: str, negated: bool = False, category: str = "diagnosis") -> dict:
    return {"category": category, "value": value, "negated": negated, "source_text": value}


def _criteria(**overrides) -> dict:
    base = {
        "trial_title": "T",
        "inclusion_quantitative": [],
        "inclusion_categorical": [],
        "exclusion_quantitative": [],
        "exclusion_categorical": [],
        "unparseable": [],
    }
    base.update(overrides)
    return base


def _patient(pid: str, **fields) -> dict:
    base = {"id": pid, "name": pid, "labs": {}, "diagnoses": [], "medications": [], "history": []}
    base.update(fields)
    return base


def _make_mapper(rules: dict[tuple[str, str], str], calls: list):
    """A stand-in for the LLM term-mapper: verdicts come from `rules`, defaulting
    to no_match; every invocation is recorded in `calls`."""

    def mapper(criterion_value: str, terms: list[str]) -> dict[str, str]:
        calls.append((criterion_value, tuple(terms)))
        cval = criterion_value.strip().lower()
        return {t.strip().lower(): rules.get((cval, t.strip().lower()), "no_match") for t in terms}

    return mapper


# --- Word-boundary fast path ------------------------------------------------


def test_fast_present_exact_whole_term():
    assert fast_present("warfarin", "warfarin") is True


def test_fast_present_specific_term_satisfies_general_criterion():
    # criterion is a prefix of a staged diagnosis, at a clean boundary
    assert fast_present("non-small cell lung cancer", "non-small cell lung cancer stage iv") is True


def test_fast_present_rejects_hyphen_compound():
    # THE regression: "small cell" must NOT fast-match inside "non-small cell ..."
    assert fast_present("small cell lung cancer", "non-small cell lung cancer stage iv") is False


def test_fast_present_absent():
    assert fast_present("multiple myeloma", "type 2 diabetes mellitus") is False


# --- small cell / non-small cell resolved via the semantic path -------------


def test_small_cell_vs_non_small_cell_resolves_via_llm():
    """Regression: a 'small cell lung cancer' inclusion must NOT match a patient
    whose diagnosis is 'non-small cell lung cancer' — the fast path defers, the
    LLM says no_match, and the patient is (correctly) ineligible."""
    crit = _criteria(inclusion_categorical=[_cat("small cell lung cancer")])
    nsclc = _patient("P1", diagnoses=["non-small cell lung cancer stage IV"])
    calls: list = []
    rules = {("small cell lung cancer", "non-small cell lung cancer stage iv"): "no_match"}
    cache = build_verdict_cache(crit, [nsclc], _make_mapper(rules, calls))

    result = evaluate_patient(nsclc, crit, cache)
    assert result["eligible"] is False
    assert result["needs_review"] is False
    assert len(calls) == 1  # the ambiguous pair went to the LLM


def test_true_small_cell_patient_matches_via_fast_path():
    """A genuine small-cell patient matches on the fast path — no LLM needed."""
    crit = _criteria(inclusion_categorical=[_cat("small cell lung cancer")])
    sclc = _patient("P2", diagnoses=["small cell lung cancer stage III"])
    calls: list = []
    cache = build_verdict_cache(crit, [sclc], _make_mapper({}, calls))

    assert cache == {}
    assert calls == []  # fast path settled it — no LLM call
    assert evaluate_patient(sclc, crit, cache)["eligible"] is True


def test_build_verdict_cache_reports_progress_per_llm_call():
    """on_progress fires once per LLM-bound criterion (the hook the matcher uses
    to keep the approve SSE stream alive between slow calls)."""
    crit = _criteria(
        inclusion_categorical=[_cat("small cell lung cancer")],
        exclusion_categorical=[_cat("prior platinum chemotherapy", category="prior_treatment")],
    )
    patient = _patient("P1", diagnoses=["non-small cell lung cancer"], medications=["carboplatin"])
    calls: list = []
    ticks: list[tuple[int, int]] = []
    build_verdict_cache(
        crit,
        [patient],
        _make_mapper({}, calls),
        on_progress=lambda done, total: ticks.append((done, total)),
    )
    # One tick per LLM call, counting up, with a stable denominator.
    assert len(ticks) == len(calls) == 2
    assert ticks == [(0, 2), (1, 2)]


# --- Semantic equivalence (platinum / carboplatin) --------------------------


def test_semantic_equivalence_matches():
    crit = _criteria(
        inclusion_categorical=[_cat("prior platinum chemotherapy", category="prior_treatment")]
    )
    patient = _patient("P3", medications=["carboplatin, 2023-04"])
    calls: list = []
    rules = {("prior platinum chemotherapy", "carboplatin, 2023-04"): "match"}
    cache = build_verdict_cache(crit, [patient], _make_mapper(rules, calls))

    assert evaluate_patient(patient, crit, cache)["eligible"] is True


# --- Uncertain -> needs review ----------------------------------------------


def test_uncertain_verdict_routes_to_needs_review():
    crit = _criteria(inclusion_categorical=[_cat("autoimmune disease", category="condition")])
    patient = _patient("P4", history=["chronic inflammatory condition, unspecified"])
    rules = {("autoimmune disease", "chronic inflammatory condition, unspecified"): "uncertain"}
    cache = build_verdict_cache(crit, [patient], _make_mapper(rules, []))

    result = evaluate_patient(patient, crit, cache)
    assert result["needs_review"] is True
    assert result["eligible"] is False  # never a silent pass/fail


def test_unavailable_llm_degrades_to_needs_review():
    """A down backend during matching marks the ambiguous tail uncertain, so the
    affected patients need review rather than being silently passed or failed."""

    def down_mapper(_c: str, _t: list[str]) -> dict[str, str]:
        raise LLMUnavailableError("backend down")

    crit = _criteria(exclusion_categorical=[_cat("active malignancy", category="condition")])
    patient = _patient("P5", history=["history of neoplasm, site unspecified"])
    cache = build_verdict_cache(crit, [patient], down_mapper)

    assert evaluate_patient(patient, crit, cache)["needs_review"] is True


def test_malformed_mapping_degrades_to_needs_review():
    """Malformed structured output must degrade like a down backend, not crash the
    /approve request (which the Parser and Critic also handle gracefully)."""
    from langchain_core.exceptions import OutputParserException

    def broken_mapper(_c: str, _t: list[str]) -> dict[str, str]:
        raise OutputParserException("model returned garbage")

    crit = _criteria(inclusion_categorical=[_cat("autoimmune disease", category="condition")])
    patient = _patient("P8", history=["chronic inflammatory condition"])
    cache = build_verdict_cache(crit, [patient], broken_mapper)

    assert evaluate_patient(patient, crit, cache)["needs_review"] is True


def test_empty_criterion_value_matches_no_one():
    assert fast_present("", "warfarin") is False


# --- Cache: one batch per criterion, not per patient ------------------------


def test_verdicts_cached_across_patients():
    """The same (criterion, term) pair recurs across patients but is asked once."""
    crit = _criteria(inclusion_categorical=[_cat("egfr mutation", category="biomarker")])
    patients = [_patient(f"P{i}", diagnoses=["EGFR L858R alteration"]) for i in range(5)]
    calls: list = []
    rules = {("egfr mutation", "egfr l858r alteration"): "match"}
    cache = build_verdict_cache(crit, patients, _make_mapper(rules, calls))

    assert len(calls) == 1  # one batch for the whole cohort
    assert cache[("egfr mutation", "egfr l858r alteration")] == "match"
    assert all(evaluate_patient(p, crit, cache)["eligible"] for p in patients)


def test_llm_calls_are_bounded_by_distinct_criteria_not_by_patient_count():
    """The caching claim, pinned (#101).

    This is the architectural assertion the whole project rests on: a model is
    consulted once per *distinct criterion*, and its verdicts are reused across
    every patient in the cohort. The regression it guards against is subtle and
    expensive — move the mapper call inside the per-patient loop and every test
    above still passes, the cohort still comes out right, and the bill silently
    goes up by two orders of magnitude.

    So the assertion is on the shape of the cost, not on a magic number: the same
    two criteria against 5 patients and against 200 make exactly the same calls.
    """
    crit = _criteria(
        inclusion_categorical=[_cat("egfr mutation", category="biomarker")],
        exclusion_categorical=[_cat("prior immunotherapy")],
    )
    rules = {("egfr mutation", "egfr l858r alteration"): "match"}

    def calls_for(cohort_size: int) -> list:
        patients = [
            _patient(f"P{i}", diagnoses=["EGFR L858R alteration"], history=["pembrolizumab"])
            for i in range(cohort_size)
        ]
        calls: list = []
        build_verdict_cache(crit, patients, _make_mapper(rules, calls))
        return calls

    small, large = calls_for(5), calls_for(200)
    # One batch per criterion with an ambiguous tail — and the cohort's size does
    # not enter into it.
    assert len(small) == 2
    assert len(large) == len(small)
    # Same distinct terms asked about, whether five patients share them or two
    # hundred do.
    assert {criterion for criterion, _ in large} == {"egfr mutation", "prior immunotherapy"}


def test_the_cache_reports_what_it_saved(monkeypatch):
    """The hit rate the metrics page shows (#101), at its source.

    `resolutions` is how many `(criterion, term)` questions the cohort's records
    raise; `llm_pairs` is how many reached a model. With 10 patients sharing 2
    terms across 1 criterion the cohort raises 20 questions and the model is asked
    2 — which is the saving, stated as the number the summary divides.
    """
    crit = _criteria(inclusion_categorical=[_cat("egfr mutation", category="biomarker")])
    patients = [
        _patient(f"P{i}", diagnoses=["EGFR L858R alteration"], history=["aspirin"])
        for i in range(10)
    ]
    recorded: list = []
    build_verdict_cache(
        crit, patients, _make_mapper({}, []), on_cost=lambda cost: recorded.append(cost)
    )

    assert len(recorded) == 1
    cost = recorded[0]
    assert cost.llm_pairs == 2  # two distinct terms, one criterion
    assert cost.resolutions == 20  # ten patients × those two terms
    assert cost.resolutions > cost.llm_pairs


def test_a_term_listed_twice_by_one_patient_counts_once(monkeypatch):
    """`patient_terms` concatenates three record fields, so one patient can list
    the same drug twice. The saving is counted per patient asking the question,
    not per mention — otherwise a messy record would inflate the hit rate."""
    crit = _criteria(inclusion_categorical=[_cat("prior platinum chemotherapy")])
    patient = _patient("P1", medications=["carboplatin"], history=["carboplatin"])
    recorded: list = []
    build_verdict_cache(
        crit, [patient], _make_mapper({}, []), on_cost=lambda cost: recorded.append(cost)
    )
    assert recorded[0].resolutions == 1
    assert recorded[0].llm_pairs == 1


def test_no_categoricals_makes_no_llm_call():
    crit = _criteria(
        inclusion_quantitative=[
            {
                "attribute": "age",
                "operator": ">=",
                "value": 18,
                "value_high": None,
                "unit": "years",
                "source_text": "age >= 18",
            }
        ]
    )
    calls: list = []
    cache = build_verdict_cache(
        crit, [_patient("P6", diagnoses=["anything"])], _make_mapper({}, calls)
    )
    assert cache == {}
    assert calls == []


# --- _map_terms_via_llm wiring ----------------------------------------------


def test_map_terms_via_llm_normalizes_and_maps(monkeypatch):
    mapping = TermMapping(
        results=[
            TermMatch(term="Carboplatin, 2023-04", verdict="match"),
            TermMatch(term="Aspirin", verdict="no_match"),
        ]
    )
    monkeypatch.setattr(matcher_mod, "get_llm", lambda: FakeChatModel([mapping]))

    out = matcher_mod._map_terms_via_llm(
        "prior platinum chemotherapy", ["Carboplatin, 2023-04", "Aspirin"]
    )
    assert out == {"carboplatin, 2023-04": "match", "aspirin": "no_match"}


def test_omitted_terms_default_to_no_match():
    """A term the mapper doesn't return is treated as no_match (the model saw it)."""
    crit = _criteria(inclusion_categorical=[_cat("diabetes")])
    patient = _patient("P7", diagnoses=["hypertension"])

    def forgetful_mapper(_c: str, _t: list[str]) -> dict[str, str]:
        return {}  # returns nothing

    cache = build_verdict_cache(crit, [patient], forgetful_mapper)
    assert cache[("diabetes", "hypertension")] == "no_match"
    assert evaluate_patient(patient, crit, cache)["eligible"] is False


# --- Durable cross-run cache tests (#105) -----------------------------------


def test_durable_cache_second_identical_run_makes_zero_llm_calls():
    """Acceptance Criterion 1 & 6: Term mappings persist across runs, and a second
    identical run makes ZERO term-mapping LLM calls."""
    from app.persistence import InMemoryTermStore

    store = InMemoryTermStore()
    crit = _criteria(inclusion_categorical=[_cat("prior platinum chemotherapy")])
    patients = [
        _patient("P1", medications=["carboplatin"]),
        _patient("P2", medications=["cisplatin"]),
    ]

    calls_run1: list = []
    rules = {
        ("prior platinum chemotherapy", "carboplatin"): "match",
        ("prior platinum chemotherapy", "cisplatin"): "match",
    }
    cost_run1: list = []
    cache1 = build_verdict_cache(
        crit,
        patients,
        _make_mapper(rules, calls_run1),
        store=store,
        model_id="test-model",
        on_cost=lambda c: cost_run1.append(c),
    )
    assert len(calls_run1) == 1
    assert len(calls_run1[0][1]) == 2
    assert cache1[("prior platinum chemotherapy", "carboplatin")] == "match"
    assert cache1[("prior platinum chemotherapy", "cisplatin")] == "match"
    assert cost_run1[0].llm_pairs == 2
    assert cost_run1[0].resolutions == 2

    # Second identical run with the same durable store
    calls_run2: list = []
    cost_run2: list = []
    cache2 = build_verdict_cache(
        crit,
        patients,
        _make_mapper(rules, calls_run2),
        store=store,
        model_id="test-model",
        on_cost=lambda c: cost_run2.append(c),
    )
    assert len(calls_run2) == 0  # ZERO LLM calls!
    assert cache2[("prior platinum chemotherapy", "carboplatin")] == "match"
    assert cache2[("prior platinum chemotherapy", "cisplatin")] == "match"
    assert cost_run2[0].llm_pairs == 0  # 0 LLM pairs sent
    assert cost_run2[0].resolutions == 2  # 2 resolutions served from cache


def test_durable_cache_persists_uncertain_verdicts():
    """Acceptance Criterion 2: 'uncertain' verdicts are cached too so an ambiguous
    pair is not re-asked on every run."""
    from app.persistence import InMemoryTermStore

    store = InMemoryTermStore()
    crit = _criteria(inclusion_categorical=[_cat("hypertension with complications")])
    patient = _patient("P1", diagnoses=["mild hypertension"])

    calls1: list = []
    rules = {("hypertension with complications", "mild hypertension"): "uncertain"}
    cache1 = build_verdict_cache(
        crit, [patient], _make_mapper(rules, calls1), store=store, model_id="test-model"
    )
    assert cache1[("hypertension with complications", "mild hypertension")] == "uncertain"
    assert len(calls1) == 1

    # Second run: uncertain verdict served from durable cache without asking LLM
    calls2: list = []
    cache2 = build_verdict_cache(
        crit, [patient], _make_mapper(rules, calls2), store=store, model_id="test-model"
    )
    assert cache2[("hypertension with complications", "mild hypertension")] == "uncertain"
    assert len(calls2) == 0


def test_durable_cache_model_isolation():
    """Acceptance Criterion 1: Keyed by model_id — a model change must not silently
    reuse another model's clinical judgement."""
    from app.persistence import InMemoryTermStore

    store = InMemoryTermStore()
    crit = _criteria(inclusion_categorical=[_cat("prior platinum chemotherapy")])
    patient = _patient("P1", medications=["carboplatin"])

    calls_model_a: list = []
    rules_a = {("prior platinum chemotherapy", "carboplatin"): "match"}
    build_verdict_cache(
        crit, [patient], _make_mapper(rules_a, calls_model_a), store=store, model_id="model-alpha"
    )
    assert len(calls_model_a) == 1

    # Run with different model: must NOT reuse model-alpha's cached judgement
    calls_model_b: list = []
    rules_b = {("prior platinum chemotherapy", "carboplatin"): "no_match"}
    cache_b = build_verdict_cache(
        crit, [patient], _make_mapper(rules_b, calls_model_b), store=store, model_id="model-beta"
    )
    assert len(calls_model_b) == 1  # model-beta was asked
    assert cache_b[("prior platinum chemotherapy", "carboplatin")] == "no_match"


def test_durable_cache_backend_outage_degrades_gracefully():
    """Acceptance Criterion 5: The in-process per-screening cache remains the first
    tier, so a cache backend outage degrades to in-process behaviour rather than
    failing a run."""

    class BrokenTermStore:
        async def get_many(self, pairs, model_id):
            raise RuntimeError("Database connection lost")

        async def set_many(self, records):
            raise RuntimeError("Disk full")

        def get_cached(self, pairs, model_id):
            raise RuntimeError("Database connection lost")

        def set_cached(self, records):
            raise RuntimeError("Disk full")

    store = BrokenTermStore()
    crit = _criteria(inclusion_categorical=[_cat("prior platinum chemotherapy")])
    patient = _patient("P1", medications=["carboplatin"])

    calls: list = []
    rules = {("prior platinum chemotherapy", "carboplatin"): "match"}
    # Must NOT raise exception despite broken store
    cache = build_verdict_cache(
        crit,
        [patient],
        _make_mapper(rules, calls),
        store=store,
        model_id="test-model",  # type: ignore[arg-type]
    )
    assert cache[("prior platinum chemotherapy", "carboplatin")] == "match"
    assert len(calls) == 1
