"""Agent 4: Patient Matcher — deterministic comparison against the synthetic EHR.

No LLM calls per patient: the typed criteria contract makes matching pure
Python. Missing lab values yield "unknown" (needs human review), never a
silent pass or fail.
"""

import json
from operator import eq, ge, gt, le, lt

from app.config import get_settings
from app.exceptions import DataStoreError
from app.graph.state import ScreenerState, event
from app.logging_config import get_logger

OPS = {">=": ge, "<=": le, ">": gt, "<": lt, "==": eq}

log = get_logger("matcher")


def _check_quantitative(patient: dict, criterion: dict) -> str:
    value = patient["labs"].get(criterion["attribute"])
    if value is None:
        return "unknown"
    if criterion["operator"] == "between":
        ok = criterion["value"] <= value <= criterion["value_high"]
    else:
        ok = OPS[criterion["operator"]](value, criterion["value"])
    return "pass" if ok else "fail"


def _categorical_present(patient: dict, criterion: dict) -> bool:
    """Whether the criterion's term appears anywhere in the patient's records."""
    haystack = " ".join(
        patient.get("diagnoses", []) + patient.get("medications", []) + patient.get("history", [])
    ).lower()
    return criterion["value"].lower() in haystack


def _check_categorical(patient: dict, criterion: dict) -> str:
    """Inclusion-side check: does the patient satisfy this categorical criterion?

    `negated` ("patient must NOT have this") is an inclusion-side concept — see
    the exclusion loop in evaluate_patient for why it is not honored there.
    """
    present = _categorical_present(patient, criterion)
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
    # Presence of an excluded term fails the patient. We match on presence and
    # ignore the criterion's `negated` flag on purpose: the exclusion list
    # already carries the "must not have" meaning, so also honoring `negated`
    # here would double-negate — wrongly failing every patient who LACKS the
    # excluded condition whenever the parser sets negated=True on an exclusion.
    for c in criteria["exclusion_categorical"]:
        results.append(
            {
                "criterion": c,
                "kind": "exclusion",
                "status": "fail" if _categorical_present(patient, c) else "pass",
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


def load_patients() -> list[dict]:
    """Read the synthetic EHR; a missing or corrupt file is a DataStoreError.

    Raised (not absorbed into state) on purpose: the matcher only runs inside
    the synchronous /approve request, where the FastAPI handler turns this
    into a 503 and the checkpointed screening stays resumable at the gate.
    """
    path = get_settings().patients_path
    try:
        patients = json.loads(path.read_text())
    except OSError as exc:
        raise DataStoreError(f"Patient records unavailable at {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise DataStoreError(f"Patient records at {path} are not valid JSON: {exc}") from exc
    if not isinstance(patients, list):
        raise DataStoreError(f"Patient records at {path} must be a JSON array of patients")
    return patients


def matcher_node(state: ScreenerState) -> dict:
    criteria = state["parsed_criteria"]
    assert criteria is not None, "matcher runs after parser — parsed_criteria is set"
    patients = load_patients()
    evaluations = [evaluate_patient(p, criteria) for p in patients]
    eligible = [e for e in evaluations if e["eligible"] and not e["needs_review"]]
    review = [e for e in evaluations if e["needs_review"]]
    log.info(
        "matcher.screened",
        patients=len(evaluations),
        eligible=len(eligible),
        needs_review=len(review),
    )
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
