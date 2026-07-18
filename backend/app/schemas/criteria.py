"""Typed criteria contracts — the interface between the Parser and the Matcher.

The closed `attribute` vocabulary is what lets the Matcher run as pure Python:
it looks up patient[attribute] and applies the operator, no LLM involved.
Every criterion carries the verbatim protocol sentence it came from, so a
reviewer can audit any threshold back to its source.
"""

from typing import Literal

from pydantic import BaseModel, Field

EhrAttribute = Literal[
    "age",
    "egfr",
    "creatinine",
    "systolic_bp",
    "diastolic_bp",
    "hba1c",
    "bmi",
    "anc",
    "platelets",
    "ecog",
    "ejection_fraction",
]


class QuantitativeCriterion(BaseModel):
    """A criterion checkable numerically against an EHR field."""

    attribute: EhrAttribute = Field(
        description="Canonical EHR attribute this constraint applies to"
    )
    operator: Literal[">=", "<=", ">", "<", "==", "between"]
    value: float
    value_high: float | None = Field(None, description="Upper bound when operator is 'between'")
    unit: str = Field(description="e.g. 'mL/min/1.73m2', 'mmHg', '%'")
    source_text: str = Field(description="Verbatim protocol sentence this was extracted from")


class CategoricalCriterion(BaseModel):
    """A criterion checked against categorical EHR data (diagnoses, meds, history)."""

    category: Literal["diagnosis", "prior_treatment", "medication", "biomarker", "condition"]
    value: str = Field(
        description="Normalized term, e.g. 'EGFR exon 19 deletion', 'prior platinum chemotherapy'"
    )
    negated: bool = Field(
        description="Inclusion-side only: True for an inclusion criterion the patient must NOT "
        "meet (e.g. 'no active infection'). Leave False for exclusion-list items — the "
        "exclusion list already means 'must not have this'."
    )
    source_text: str


class CriteriaSchema(BaseModel):
    """Full structured extraction of a protocol's eligibility section."""

    trial_title: str
    inclusion_quantitative: list[QuantitativeCriterion]
    inclusion_categorical: list[CategoricalCriterion]
    exclusion_quantitative: list[QuantitativeCriterion]
    exclusion_categorical: list[CategoricalCriterion]
    unparseable: list[str] = Field(
        description="Criteria that could not be converted to structured form — verbatim text. "
        "Never invent numbers; put vague criteria here instead."
    )
