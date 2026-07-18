"""Agent 4: Patient Matcher — deterministic comparison + a thin semantic tail.

Quantitative checks are pure Python: the typed criteria contract makes them a
lookup and an operator, no LLM involved. Categorical checks resolve in two
tiers: a word-boundary fast path settles the clear cases (an exact term match,
a clear absence) with zero LLM calls, and only the ambiguous tail — a partial
overlap like "small cell" inside "non-small cell lung cancer", or a semantic
equivalence like "prior platinum chemotherapy" vs "carboplatin" — goes to an
LLM term-mapping step.

Those mappings are computed once per screening and cached by
`(criterion_value, patient_term)`: the same pairs recur across all 100 patients,
so the cost is one batch of calls per distinct criterion, never per patient. An
"uncertain" verdict (and an unavailable LLM) yields "unknown" → needs human
review, never a silent pass or fail. Missing lab values do the same.
"""

import json
import re
from collections.abc import Callable
from operator import eq, ge, gt, le, lt

from langchain_core.exceptions import OutputParserException
from pydantic import ValidationError

from app.config import get_settings
from app.exceptions import DataStoreError, LLMUnavailableError
from app.graph.state import ScreenerState, event
from app.logging_config import get_logger
from app.schemas.review import TermMapping
from app.services.llm import get_llm, invoke_with_retry

OPS = {">=": ge, "<=": le, ">": gt, "<": lt, "==": eq}

log = get_logger("matcher")

MATCHER_SYSTEM = """You are a clinical terminology matcher. Given a single trial \
eligibility criterion and a list of terms drawn from ONE patient's records (diagnoses, \
medications, medical history), decide for EACH term whether it satisfies the criterion.

Return a verdict per term:
- "match": the term denotes the clinical concept the criterion requires. A more specific \
term satisfies a more general criterion — e.g. criterion "prior platinum chemotherapy" \
vs term "carboplatin, 2023-04"; criterion "non-small cell lung cancer" vs term "NSCLC \
stage IV".
- "no_match": a different concept, even when the wording overlaps — e.g. criterion \
"small cell lung cancer" vs term "non-small cell lung cancer" are OPPOSITE diagnoses.
- "uncertain": you cannot decide confidently from the term alone.

Judge only clinical equivalence; never infer facts the term does not state. Echo each \
term back verbatim."""

# Type of the term-mapping callable: (criterion_value, patient_terms) -> {norm_term: verdict}
TermMapper = Callable[[str, list[str]], dict[str, str]]


def _norm(text: str) -> str:
    return text.strip().lower()


def _patient_terms(patient: dict) -> list[str]:
    terms: list[str] = []
    for field in ("diagnoses", "medications", "history"):
        terms.extend(patient.get(field, []))
    return terms


def _fast_present(criterion_value: str, term: str) -> bool:
    """Word-boundary fast path: is `criterion_value` a confident match for `term`?

    Both are already normalized. We require a word-boundary occurrence AND that it
    is not glued into a larger hyphen compound: "non-small cell lung cancer" must
    NOT fast-match a "small cell" criterion (the hyphen before "small" is the tell),
    but "non-small cell lung cancer stage IV" DOES match a "non-small cell lung
    cancer" criterion. Anything the fast path can't settle falls through to the LLM.
    """
    if not criterion_value:
        # An empty value would make the \b\b regex match at position 0 and mark
        # the criterion present for everyone; treat it as never a confident match.
        return False
    for m in re.finditer(rf"\b{re.escape(criterion_value)}\b", term):
        before = term[m.start() - 1] if m.start() > 0 else ""
        after = term[m.end()] if m.end() < len(term) else ""
        if before == "-" or after == "-":
            continue  # part of a hyphen compound (e.g. "non-small") — not confident
        return True
    return False


def _check_quantitative(patient: dict, criterion: dict) -> str:
    value = patient["labs"].get(criterion["attribute"])
    if value is None:
        return "unknown"
    if criterion["operator"] == "between":
        ok = criterion["value"] <= value <= criterion["value_high"]
    else:
        ok = OPS[criterion["operator"]](value, criterion["value"])
    return "pass" if ok else "fail"


def _categorical_presence(patient: dict, criterion: dict, verdicts: dict) -> str:
    """Resolve a criterion against a patient's terms: 'present' | 'absent' | 'uncertain'.

    `verdicts` is the screening-wide cache keyed by `(criterion_value, term)` (both
    normalized) holding LLM verdicts for the ambiguous tail. When it is empty (unit
    tests, or the LLM step was skipped) only the deterministic fast path applies.
    """
    cval = _norm(criterion["value"])
    result = "absent"
    for term in _patient_terms(patient):
        tnorm = _norm(term)
        if _fast_present(cval, tnorm):
            return "present"
        verdict = verdicts.get((cval, tnorm))
        if verdict == "match":
            return "present"
        if verdict == "uncertain":
            result = "uncertain"
    return result


def _check_categorical(patient: dict, criterion: dict, verdicts: dict) -> str:
    """Inclusion-side check: does the patient satisfy this categorical criterion?

    `negated` ("patient must NOT have this") is an inclusion-side concept — see
    the exclusion loop in evaluate_patient for why it is not honored there.
    """
    presence = _categorical_presence(patient, criterion, verdicts)
    if presence == "uncertain":
        return "unknown"
    if criterion["negated"]:
        return "fail" if presence == "present" else "pass"
    return "pass" if presence == "present" else "fail"


def evaluate_patient(patient: dict, criteria: dict, verdicts: dict | None = None) -> dict:
    verdicts = verdicts or {}
    results = []
    for c in criteria["inclusion_quantitative"]:
        results.append(
            {"criterion": c, "kind": "inclusion", "status": _check_quantitative(patient, c)}
        )
    for c in criteria["inclusion_categorical"]:
        status = _check_categorical(patient, c, verdicts)
        results.append({"criterion": c, "kind": "inclusion", "status": status})
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
        presence = _categorical_presence(patient, c, verdicts)
        if presence == "uncertain":
            status = "unknown"
        else:
            status = "fail" if presence == "present" else "pass"
        results.append({"criterion": c, "kind": "exclusion", "status": status})

    known = [r for r in results if r["status"] != "unknown"]
    return {
        "patient_id": patient["id"],
        "name": patient.get("name"),
        "eligible": bool(known) and all(r["status"] == "pass" for r in known),
        "needs_review": any(r["status"] == "unknown" for r in results),
        "criterion_results": results,
    }


def _map_terms_via_llm(criterion_value: str, terms: list[str]) -> dict[str, str]:
    """One LLM call: classify every candidate `term` against `criterion_value`.

    Returns `{normalized_term: verdict}`. Terms the model omits default to
    "no_match" (the model saw them and did not flag a match).
    """
    structured = get_llm().with_structured_output(TermMapping)
    numbered = "\n".join(f"- {t}" for t in terms)
    prompt = f"Criterion: {criterion_value}\n\nPatient terms:\n{numbered}\n\nClassify every term."
    messages = [("system", MATCHER_SYSTEM), ("user", prompt)]
    raw = invoke_with_retry(structured, messages)
    mapping = raw if isinstance(raw, TermMapping) else TermMapping.model_validate(raw)
    return {_norm(r.term): r.verdict for r in mapping.results}


def build_verdict_cache(
    criteria: dict, patients: list[dict], mapper: TermMapper = _map_terms_via_llm
) -> dict[tuple[str, str], str]:
    """Resolve the ambiguous categorical tail once for the whole cohort.

    For each distinct categorical criterion, gather every patient term the fast
    path can't already settle, ask the mapper in one batch, and cache the verdict
    per `(criterion_value, term)`. An unavailable LLM degrades to "uncertain" for
    that criterion's terms → the affected patients land in needs-review rather than
    being silently passed or failed.
    """
    categoricals = criteria["inclusion_categorical"] + criteria["exclusion_categorical"]
    # One representative original spelling per normalized term, for the prompt.
    term_by_norm: dict[str, str] = {}
    for p in patients:
        for t in _patient_terms(p):
            term_by_norm.setdefault(_norm(t), t)

    cache: dict[tuple[str, str], str] = {}
    for c in categoricals:
        cval = _norm(c["value"])
        candidates = {
            tnorm: original
            for tnorm, original in term_by_norm.items()
            if not _fast_present(cval, tnorm) and (cval, tnorm) not in cache
        }
        if not candidates:
            continue
        try:
            verdicts = mapper(c["value"], list(candidates.values()))
        except LLMUnavailableError as exc:
            log.warning("matcher.term_mapping_unavailable", criterion=c["value"], detail=str(exc))
            verdicts = {tnorm: "uncertain" for tnorm in candidates}
        except (ValidationError, OutputParserException) as exc:
            # Malformed structured output must degrade, not 500 the /approve request
            # (the Critic and Parser handle the same case). Safe fallback is
            # "uncertain" → needs review, never a silent pass/fail.
            log.warning(
                "matcher.term_mapping_invalid", criterion=c["value"], error=type(exc).__name__
            )
            verdicts = {tnorm: "uncertain" for tnorm in candidates}
        for tnorm in candidates:
            cache[(cval, tnorm)] = verdicts.get(tnorm, "no_match")
    return cache


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
    verdicts = build_verdict_cache(criteria, patients)
    evaluations = [evaluate_patient(p, criteria, verdicts) for p in patients]
    eligible = [e for e in evaluations if e["eligible"] and not e["needs_review"]]
    review = [e for e in evaluations if e["needs_review"]]
    log.info(
        "matcher.screened",
        patients=len(evaluations),
        eligible=len(eligible),
        needs_review=len(review),
        semantic_pairs=len(verdicts),
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
