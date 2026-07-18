"""Edge cases (#9): degenerate inputs the pipeline must handle without crashing —
a PDF with no eligibility section, an empty patient database, and criteria where
every patient lands in needs-review.
"""

from typing import cast

import pymupdf

import app.graph.nodes.matcher as matcher_mod
from app.graph.nodes.matcher import evaluate_patient, matcher_node
from app.graph.state import ScreenerState
from app.services.pdf import extract_eligibility_text

# --- PDF extraction on real (in-memory) PDFs -------------------------------


def _pdf(*page_texts: str) -> bytes:
    doc = pymupdf.open()
    for text in page_texts:
        page = doc.new_page()
        page.insert_text((72, 72), text)
    return bytes(doc.tobytes())


def test_pdf_with_eligibility_section_returns_windowed_text():
    pdf = _pdf(
        "Title page. Sponsor and protocol number.",
        "Background and rationale for the study.",
        "Inclusion criteria: age 18 or older. Exclusion criteria: pregnancy. Eligibility "
        "assessed at screening.",
        "Statistical analysis plan and endpoints.",
    )
    text = extract_eligibility_text(pdf)
    assert "Inclusion criteria" in text
    assert "Exclusion criteria" in text


def test_pdf_without_eligibility_section_returns_all_text():
    # No section hints anywhere: the extractor returns everything and lets the
    # Router flag it as suspicious rather than dropping content.
    pdf = _pdf("Quarterly logistics report.", "Coffee supplies and parking notes.")
    text = extract_eligibility_text(pdf)
    assert "logistics" in text
    assert "parking" in text


# --- 0-patient database ----------------------------------------------------

_CRITERIA = {
    "trial_title": "T",
    "inclusion_quantitative": [
        {
            "attribute": "age",
            "operator": ">=",
            "value": 18,
            "value_high": None,
            "unit": "years",
            "source_text": "Age >= 18",
        }
    ],
    "inclusion_categorical": [],
    "exclusion_quantitative": [],
    "exclusion_categorical": [],
    "unparseable": [],
}

_STATE: ScreenerState = {
    "raw_protocol_text": "x",
    "source_filename": "p.md",
    "parsed_criteria": _CRITERIA,
    "compliance_passed": True,
    "critic_feedback": None,
    "parse_attempts": 1,
    "compliance_findings": [],
    "matched_patients": [],
    "events": [],
    "current_step": "matching",
}


def test_matcher_on_empty_database_completes_with_no_matches(monkeypatch):
    monkeypatch.setattr(matcher_mod, "load_patients", lambda: [])
    update = matcher_node(_STATE)
    assert update["matched_patients"] == []
    assert update["current_step"] == "done"
    assert "0 eligible" in update["events"][0]["detail"]


# --- Every patient needs review --------------------------------------------


def test_criterion_on_absent_lab_makes_every_patient_needs_review(monkeypatch):
    # The criterion checks ejection_fraction, which none of these patients have —
    # so no patient can be decided and all fall to needs_review (never a silent
    # pass or fail).
    patients = [
        {"id": "PT-1", "name": "A", "labs": {"age": 40}, "diagnoses": [], "medications": []},
        {"id": "PT-2", "name": "B", "labs": {"age": 55}, "diagnoses": [], "medications": []},
    ]
    monkeypatch.setattr(matcher_mod, "load_patients", lambda: patients)
    criteria = {
        **_CRITERIA,
        "inclusion_quantitative": [
            {
                "attribute": "ejection_fraction",
                "operator": ">=",
                "value": 50,
                "value_high": None,
                "unit": "%",
                "source_text": "LVEF >= 50%",
            }
        ],
    }
    update = matcher_node(cast(ScreenerState, {**_STATE, "parsed_criteria": criteria}))
    evaluations = update["matched_patients"]
    assert len(evaluations) == 2
    assert all(e["needs_review"] for e in evaluations)
    assert not any(e["eligible"] for e in evaluations)
    assert "2 need review" in update["events"][0]["detail"]


def test_evaluate_patient_with_no_known_criteria_is_not_eligible():
    # A patient with an empty lab set against a quantitative criterion: no known
    # results, so `eligible` is False (not vacuously True) and review is flagged.
    patient = {"id": "PT-X", "name": "X", "labs": {}, "diagnoses": [], "medications": []}
    result = evaluate_patient(patient, _CRITERIA)
    assert result["eligible"] is False
    assert result["needs_review"] is True
