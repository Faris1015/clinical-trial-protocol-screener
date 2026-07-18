"""Tests for Parser post-processing (LLM extraction is mocked elsewhere)."""

from app.graph.nodes.parser import _dedupe_categoricals
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
