"""Exhaustive tests for the Matcher — it's pure Python, so test the boundaries hard."""
from app.graph.nodes.matcher import evaluate_patient

BASE_PATIENT = {
    "id": "PT-TEST",
    "name": "Test Patient",
    "labs": {"age": 55, "egfr": 60.0, "ecog": 1},
    "diagnoses": ["non-small cell lung cancer stage IV", "EGFR exon 19 deletion"],
    "medications": [],
    "history": [],
}


def _criteria(**overrides):
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


def test_ge_boundary_inclusive():
    crit = _criteria(inclusion_quantitative=[
        {"attribute": "egfr", "operator": ">=", "value": 60, "value_high": None,
         "unit": "mL/min", "source_text": "eGFR >= 60"}])
    assert evaluate_patient(BASE_PATIENT, crit)["eligible"] is True


def test_gt_boundary_excludes_equal():
    crit = _criteria(inclusion_quantitative=[
        {"attribute": "egfr", "operator": ">", "value": 60, "value_high": None,
         "unit": "mL/min", "source_text": "eGFR > 60"}])
    assert evaluate_patient(BASE_PATIENT, crit)["eligible"] is False


def test_between_operator():
    crit = _criteria(inclusion_quantitative=[
        {"attribute": "age", "operator": "between", "value": 18, "value_high": 65,
         "unit": "years", "source_text": "18-65"}])
    assert evaluate_patient(BASE_PATIENT, crit)["eligible"] is True


def test_missing_lab_is_needs_review_not_fail():
    p = {**BASE_PATIENT, "labs": {"age": 55, "ecog": 1}}  # no egfr
    crit = _criteria(inclusion_quantitative=[
        {"attribute": "egfr", "operator": ">=", "value": 60, "value_high": None,
         "unit": "mL/min", "source_text": "eGFR >= 60"}])
    result = evaluate_patient(p, crit)
    assert result["needs_review"] is True


def test_matching_exclusion_fails_patient():
    crit = _criteria(exclusion_categorical=[
        {"category": "biomarker", "value": "EGFR exon 19 deletion", "negated": False,
         "source_text": "prior EGFR"}])
    assert evaluate_patient(BASE_PATIENT, crit)["eligible"] is False


def test_negated_categorical():
    crit = _criteria(inclusion_categorical=[
        {"category": "diagnosis", "value": "multiple myeloma", "negated": True,
         "source_text": "no multiple myeloma"}])
    # Patient does not have multiple myeloma -> negated inclusion passes
    assert evaluate_patient(BASE_PATIENT, crit)["eligible"] is True
