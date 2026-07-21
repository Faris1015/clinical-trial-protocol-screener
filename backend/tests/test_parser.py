"""Tests for Parser post-processing (LLM extraction is mocked elsewhere)."""

import pytest

from app.graph.nodes.parser import (
    _clean_source_text,
    _dedupe_categoricals,
    _normalize_source_text,
)
from app.schemas.criteria import CriteriaSchema


def _cat(value: str, negated: bool = False, category: str = "prior_treatment") -> dict:
    return {"category": category, "value": value, "negated": negated, "source_text": value}


def _schema(**overrides) -> CriteriaSchema:
    base = {
        "trial_title": "T",
        "inclusion_quantitative": [],
        "inclusion_categorical": [],
        "exclusion_quantitative": [],
        "exclusion_categorical": [],
        "unparseable": [],
    }
    base.update(overrides)
    return CriteriaSchema.model_validate(base)


def test_cross_list_duplicate_dropped_from_inclusion():
    # A GLP-1 term extracted as BOTH a negated inclusion and an exclusion.
    s = _schema(
        inclusion_categorical=[_cat("GLP-1 receptor agonist", negated=True)],
        exclusion_categorical=[_cat("GLP-1 receptor agonist")],
    )
    out = _dedupe_categoricals(s)
    assert [c.value for c in out.inclusion_categorical] == []
    assert [c.value for c in out.exclusion_categorical] == ["GLP-1 receptor agonist"]


def test_case_insensitive_and_whitespace_match():
    s = _schema(
        inclusion_categorical=[_cat("  glp-1 receptor AGONIST ", negated=True)],
        exclusion_categorical=[_cat("GLP-1 receptor agonist")],
    )
    out = _dedupe_categoricals(s)
    assert out.inclusion_categorical == []


def test_intra_list_repeats_and_empty_values_removed():
    s = _schema(
        inclusion_categorical=[
            _cat("type 2 diabetes mellitus", category="diagnosis"),
            _cat("type 2 diabetes mellitus", category="diagnosis"),  # repeat
            _cat("", category="biomarker"),  # empty junk
        ],
    )
    out = _dedupe_categoricals(s)
    assert [c.value for c in out.inclusion_categorical] == ["type 2 diabetes mellitus"]


def test_distinct_terms_preserved():
    s = _schema(
        inclusion_categorical=[_cat("type 2 diabetes mellitus", category="diagnosis")],
        exclusion_categorical=[
            _cat("GLP-1 receptor agonist"),
            _cat("pregnant or breastfeeding", category="condition"),
        ],
    )
    out = _dedupe_categoricals(s)
    assert [c.value for c in out.inclusion_categorical] == ["type 2 diabetes mellitus"]
    assert len(out.exclusion_categorical) == 2


# --- source_text normalization ---------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # The observed artifact: section header + list number folded in.
        (
            "Patients meeting any of the following are excluded: 1. Prior systemic "
            "therapy for the current malignancy within 4 weeks.",
            "Prior systemic therapy for the current malignancy within 4 weeks.",
        ),
        # Bare leading list marker, no header.
        (
            "2. Systolic blood pressure > 150 mmHg at screening.",
            "Systolic blood pressure > 150 mmHg at screening.",
        ),
        ("- Active, uncontrolled infection.", "Active, uncontrolled infection."),
        # Clean sentences are untouched.
        ("eGFR >= 60 mL/min/1.73m2.", "eGFR >= 60 mL/min/1.73m2."),
        (
            "Histologically confirmed advanced solid tumor.",
            "Histologically confirmed advanced solid tumor.",
        ),
        # False-positive guard: a mid-sentence colon NOT followed by a list item
        # must survive intact.
        ("Prior treatment completed: see appendix.", "Prior treatment completed: see appendix."),
    ],
)
def test_clean_source_text(raw: str, expected: str):
    assert _clean_source_text(raw) == expected


def test_normalize_source_text_applies_across_all_groups():
    s = _schema(
        exclusion_categorical=[
            {
                "category": "prior_treatment",
                "value": "prior systemic therapy",
                "negated": False,
                "source_text": "Patients are excluded if any apply: 1. prior systemic therapy.",
            }
        ],
        inclusion_quantitative=[
            {
                "attribute": "egfr",
                "operator": ">=",
                "value": 60,
                "value_high": None,
                "unit": "mL/min/1.73m2",
                "source_text": "eGFR >= 60 mL/min/1.73m2.",
            }
        ],
    )
    out = _normalize_source_text(s)
    assert out.exclusion_categorical[0].source_text == "prior systemic therapy."
    assert out.inclusion_quantitative[0].source_text == "eGFR >= 60 mL/min/1.73m2."  # untouched
