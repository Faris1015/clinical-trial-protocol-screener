"""Agent 4: Patient Matcher — deterministic comparison against the synthetic EHR.

No LLM calls per patient: the typed criteria contract makes matching pure
Python. Missing lab values yield "unknown" (needs human review), never a
silent pass or fail.
"""

import json
from operator import eq, ge, gt, le, lt
from pathlib import Path

from app.graph.state import ScreenerState, event

OPS = {">=": ge, "<=": le, ">": gt, "<": lt, "==": eq}
PATIENTS_PATH = Path(__file__).resolve().parents[2] / "data" / "patients.json"


def _check_quantitative(patient: dict, criterion: dict) -> str:
    value = patient["labs"].get(criterion["attribute"])
    if value is None:
        return "unknown"
    if criterion["operator"] == "between":
        ok = criterion["value"] <= value <= criterion["value_high"]
    else:
        ok = OPS[criterion["operator"]](value, criterion["value"])
    return "pass" if ok else "fail"


def _check_categorical(patient: dict, criterion: dict) -> str:
    haystack = " ".join(
        patient.get("diagnoses", []) + patient.get("medications", []) + patient.get("history", [])
    ).lower()
    present = criterion["value"].lower() in haystack
    if criterion["negated"]:
        return "fail" if present else "pass"
    return "pass" if present else "fail"


def evaluate_patient(patient: dict, criteria: dict) -> dict:
    results = []
    for c in criteria["inclusion_quantitative"]:
        results.append(
            {"criterion": c, "kind": "inclusion", "status": _check_quantitative(patient, c)}
        )
    for c in criteria["inclusion_categorical"]:
        results.append(
            {"criterion": c, "kind": "inclusion", "status": _check_categorical(patient, c)}
        )
    # A patient MATCHING an exclusion criterion fails screening
    for c in criteria["exclusion_quantitative"]:
        status = _check_quantitative(patient, c)
        results.append(
            {
                "criterion": c,
                "kind": "exclusion",
                "status": {"pass": "fail", "fail": "pass"}.get(status, status),
            }
        )
    for c in criteria["exclusion_categorical"]:
        status = _check_categorical(patient, c)
        results.append(
            {
                "criterion": c,
                "kind": "exclusion",
                "status": {"pass": "fail", "fail": "pass"}.get(status, status),
            }
        )

    known = [r for r in results if r["status"] != "unknown"]
    return {
        "patient_id": patient["id"],
        "name": patient.get("name"),
        "eligible": bool(known) and all(r["status"] == "pass" for r in known),
        "needs_review": any(r["status"] == "unknown" for r in results),
        "criterion_results": results,
    }


def matcher_node(state: ScreenerState) -> dict:
    criteria = state["parsed_criteria"]
    assert criteria is not None, "matcher runs after parser — parsed_criteria is set"
    patients = json.loads(PATIENTS_PATH.read_text())
    evaluations = [evaluate_patient(p, criteria) for p in patients]
    eligible = [e for e in evaluations if e["eligible"] and not e["needs_review"]]
    review = [e for e in evaluations if e["needs_review"]]
    return {
        "matched_patients": evaluations,
        "current_step": "done",
        "events": [
            event(
                "matcher",
                "completed",
                f"Screened {len(evaluations)} patients: {len(eligible)} eligible, "
                f"{len(review)} need review",
            )
        ],
    }
