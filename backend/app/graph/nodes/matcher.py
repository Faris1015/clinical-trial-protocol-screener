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

Every verdict also carries a plain-language layer (#52): a per-criterion
`explanation`, a per-patient `summary`, and a cohort `match_summary` — rendered
from the same deterministic comparison, so the plain view can never disagree
with the statuses beneath it.
"""

import asyncio
import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from operator import eq, ge, gt, le, lt
from typing import Any, NamedTuple

from langchain_core.exceptions import OutputParserException
from langgraph.config import get_stream_writer
from pydantic import ValidationError

from app.config import get_settings
from app.exceptions import DataStoreError, LLMUnavailableError
from app.graph.state import ScreenerState, event
from app.logging_config import get_logger
from app.persistence import TermRecord, TermStore
from app.schemas.review import TermMapping
from app.services import metrics
from app.services import terms as terms_service
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


def patient_terms(patient: dict) -> list[str]:
    """Every term of a patient's record the categorical checks look at.

    Public because reverse matching (#96) has to know which pairs a run's stored
    term mappings leave unanswered, and that is exactly this list crossed with the
    criteria — asking it of a second implementation of "what counts as a term"
    would be how a patient's rematched verdict starts differing from the cohort's.
    """
    terms: list[str] = []
    for field in ("diagnoses", "medications", "history"):
        terms.extend(patient.get(field, []))
    return terms


def fast_present(criterion_value: str, term: str) -> bool:
    """Word-boundary fast path: is `criterion_value` a confident match for `term`?

    Public for the same reason `patient_terms` is (#96): a pair this settles needs
    no mapping, so reverse matching has to apply the very same test to tell a gap
    in a run's stored mappings from a pair that was never going to need one.

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


def compare_quantitative(value: float | None, criterion: dict) -> str:
    """Apply one numeric criterion to one already-read lab value.

    Takes the value rather than the patient record so the what-if simulator (#95)
    can apply a *moved* threshold to the value the run recorded, with no patient
    record in hand. The comparison is the one rule both go through: a simulated
    cohort that
    bucketed patients by a second copy of this logic would eventually disagree
    with the run it is simulating, which is the one thing a what-if panel must
    never do.

    A missing value is "unknown" — never a silent pass or fail.
    """
    if value is None:
        return "unknown"
    if criterion["operator"] == "between":
        ok = criterion["value"] <= value <= criterion["value_high"]
    else:
        ok = OPS[criterion["operator"]](value, criterion["value"])
    return "pass" if ok else "fail"


def flip_exclusion(status: str) -> str:
    """An exclusion's inclusion-side verdict, flipped to the screening verdict.

    A patient who *matches* an exclusion criterion fails screening, so "pass"
    (the bound holds) becomes "fail". "unknown" passes straight through: an
    undecidable criterion is undecidable on either side.
    """
    return {"pass": "fail", "fail": "pass"}.get(status, status)


def _categorical_presence(patient: dict, criterion: dict, verdicts: dict) -> tuple[str, str | None]:
    """Resolve a criterion against a patient's terms.

    Returns `(presence, evidence)` where presence is 'present' | 'absent' |
    'uncertain' and evidence is the patient term that decided it, in its original
    spelling (None when nothing in the record was relevant). The evidence is what
    makes the plain-language explanation specific — "the records show
    'carboplatin, 2023-04', which counts as 'prior platinum chemotherapy'" rather
    than restating the criterion back at the reviewer (#52).

    `verdicts` is the screening-wide cache keyed by `(criterion_value, term)` (both
    normalized) holding LLM verdicts for the ambiguous tail. When it is empty (unit
    tests, or the LLM step was skipped) only the deterministic fast path applies.
    """
    cval = _norm(criterion["value"])
    result: tuple[str, str | None] = ("absent", None)
    for term in patient_terms(patient):
        tnorm = _norm(term)
        if fast_present(cval, tnorm):
            return "present", term
        verdict = verdicts.get((cval, tnorm))
        if verdict == "match":
            return "present", term
        if verdict == "uncertain" and result[0] != "uncertain":
            # First unmappable term wins as the evidence: it is the one a human
            # is being asked to judge.
            result = ("uncertain", term)
    return result


def _inclusion_status(presence: str, negated: bool) -> str:
    """Inclusion-side status for a categorical criterion.

    `negated` ("patient must NOT have this") is an inclusion-side concept — see
    the exclusion loop in evaluate_patient for why it is not honored there.
    """
    if presence == "uncertain":
        return "unknown"
    if negated:
        return "fail" if presence == "present" else "pass"
    return "pass" if presence == "present" else "fail"


# --- Plain-language layer (#52) --------------------------------------------
#
# Every verdict the matcher reaches is deterministic, so its explanation is too:
# these render the same pass/fail/unknown the technical view shows, in the words
# a coordinator would use. No LLM call — an explanation that could disagree with
# the status it explains would be worse than none at all.

# Written for mid-sentence use ("the patient's eGFR is 42"), so no sentence ever
# starts with one: capitalizing "eGFR" or "HbA1c" would mangle it.
ATTRIBUTE_LABELS = {
    "age": "age",
    "egfr": "eGFR",
    "creatinine": "creatinine",
    "systolic_bp": "systolic blood pressure",
    "diastolic_bp": "diastolic blood pressure",
    "hba1c": "HbA1c",
    "bmi": "BMI",
    "anc": "neutrophil count",
    "platelets": "platelet count",
    "ecog": "ECOG performance status",
    "ejection_fraction": "ejection fraction",
}

COMPARISON_WORDS = {">=": "at least", "<=": "at most", ">": "above", "<": "below", "==": "exactly"}


def _attribute_label(attribute: str) -> str:
    return ATTRIBUTE_LABELS.get(attribute, attribute.replace("_", " "))


def _num(value: float) -> str:
    """Drop the trailing ".0" a float threshold prints with: "at least 60", not
    "at least 60.0". Non-integral values keep their decimals."""
    return str(int(value)) if float(value).is_integer() else str(value)


def _with_unit(value: float, unit: str) -> str:
    return f"{_num(value)} {unit}".strip()


def _threshold_phrase(criterion: dict) -> str:
    if criterion["operator"] == "between":
        return (
            f"between {_num(criterion['value'])} and "
            f"{_with_unit(criterion['value_high'], criterion['unit'])}"
        )
    word = COMPARISON_WORDS[criterion["operator"]]
    return f"{word} {_with_unit(criterion['value'], criterion['unit'])}"


def _explain_quantitative(value: float | None, criterion: dict, kind: str, status: str) -> str:
    """The plain-language line for one numeric verdict.

    Takes the observed value rather than the patient record so the simulator (#95)
    can re-render an explanation for a moved threshold from the value the run
    recorded — the same reason `compare_quantitative` takes one.
    """
    label = _attribute_label(criterion["attribute"])
    threshold = _threshold_phrase(criterion)
    if value is None:
        return f"No {label} value is on file, so this could not be checked."
    reading = f"The patient's {label} is {_with_unit(value, criterion['unit'])}"
    if kind == "inclusion":
        if status == "pass":
            return f"{reading}, and the trial asks for {threshold}."
        return f"{reading}, but the trial asks for {threshold}."
    # Exclusion: `status` is already flipped, so "fail" means the patient hits
    # the exclusion and is ruled out by it.
    if status == "fail":
        return f"{reading}, which the trial excludes ({threshold})."
    return f"{reading}, and the trial only excludes {threshold} — so this does not rule them out."


def _explain_categorical(criterion: dict, kind: str, presence: str, evidence: str | None) -> str:
    term = criterion["value"]
    if presence == "uncertain":
        # An uncertain presence always names the term that caused it (see
        # _categorical_presence); `or term` is belt-and-braces, not a real case.
        return (
            f"The records mention “{evidence or term}”, and whether that counts as “{term}” "
            "could not be decided automatically — someone has to judge it."
        )

    if presence == "present":
        shown = (
            f"The records show “{evidence}”, which counts as “{term}”"
            if evidence and _norm(evidence) != _norm(term)
            else f"The records show “{term}”"
        )
        if kind == "exclusion":
            return f"{shown}, which the trial excludes."
        if criterion["negated"]:
            return f"{shown}, and the trial requires patients not to have it."
        return f"{shown}, which the trial requires."

    nothing = f"Nothing in the records points to “{term}”"
    if kind == "exclusion":
        return f"{nothing}, so this exclusion does not apply."
    if criterion["negated"]:
        return f"{nothing}, which is what the trial requires."
    return f"{nothing}, and the trial requires it."


def _criterion_label(criterion: dict) -> str:
    """A short name for a criterion — an attribute for a lab bound, the term
    itself for a categorical one. Used to name criteria inside a summary."""
    if "attribute" in criterion:
        return _attribute_label(criterion["attribute"])
    return str(criterion["value"])


def _criterion_names(results: list[dict], limit: int = 2) -> str:
    """Name the criteria behind a verdict, capped so a summary stays one line.

    The cap is why the technical view exists: a patient failing six criteria gets
    two named here and the full list one click away, never a silently truncated
    "the reason is X" that hides five others.
    """
    labels = [_criterion_label(r["criterion"]) for r in results]
    shown = labels[:limit]
    if len(labels) > limit:
        shown.append(f"and {len(labels) - limit} more")
    return ", ".join(shown)


def summarize_patient(who: str, results: list[dict], eligible: bool, needs_review: bool) -> str:
    """One plain-language line per patient: the verdict and why (#52)."""
    if not results:
        return f"{who} could not be screened — this protocol has no criteria to check against."

    inclusions = [r for r in results if r["kind"] == "inclusion"]
    exclusions = [r for r in results if r["kind"] == "exclusion"]
    unknown = [r for r in results if r["status"] == "unknown"]
    failed = [r for r in results if r["status"] == "fail"]

    # Needs-review outranks the verdict, exactly as the UI's buckets do: a
    # patient with an unresolved criterion has no final answer yet.
    if needs_review:
        counted = (
            "the one criterion"
            if len(results) == 1
            else f"{len(unknown)} of {len(results)} criteria"
        )
        return (
            f"{who} needs a human check — {counted} could not be judged from the records "
            f"({_criterion_names(unknown)})."
        )

    if eligible:
        parts = []
        if len(inclusions) == 1:
            parts.append("meets the one inclusion criterion")
        elif inclusions:
            parts.append(f"meets all {len(inclusions)} inclusion criteria")
        if len(exclusions) == 1:
            parts.append(f"the one exclusion ({_criterion_names(exclusions)}) does not apply")
        elif exclusions:
            parts.append(f"none of the {len(exclusions)} exclusions apply")
        return f"{who} matches — " + "; ".join(parts) + "."

    reasons = []
    failed_inclusions = [r for r in failed if r["kind"] == "inclusion"]
    failed_exclusions = [r for r in failed if r["kind"] == "exclusion"]
    if failed_inclusions:
        # "1 of 1 inclusion criteria" is technically true and reads like a bug, so
        # a single-criterion protocol gets its own wording.
        counted = (
            "the one inclusion criterion"
            if len(inclusions) == 1
            else f"{len(failed_inclusions)} of {len(inclusions)} inclusion criteria"
        )
        reasons.append(f"does not meet {counted} ({_criterion_names(failed_inclusions)})")
    if failed_exclusions:
        noun = "exclusion" if len(failed_exclusions) == 1 else "exclusions"
        reasons.append(
            f"is ruled out by {len(failed_exclusions)} {noun} "
            f"({_criterion_names(failed_exclusions)})"
        )
    # Ineligible without a failing criterion is impossible: an undecided criterion
    # is "unknown", which the needs-review branch above already returned on.
    assert reasons, "an ineligible patient always fails at least one criterion"
    return f"{who} does not match — " + "; ".join(reasons) + "."


def summarize_cohort(evaluations: list[dict]) -> str:
    """The cohort split in one plain-language line (#52)."""
    total = len(evaluations)
    if not total:
        return "No patient records were available to screen."
    review = sum(1 for e in evaluations if e["needs_review"])
    matched = sum(1 for e in evaluations if e["eligible"] and not e["needs_review"])
    # Same three buckets the cohort table shows, in the same precedence.
    no_match = total - matched - review
    noun = "patient" if total == 1 else "patients"
    # Participle phrases, not verbs: "1 match this protocol" reads as a typo, and
    # the counts are whatever the cohort happens to be.
    return (
        f"Screened {total} {noun}: {matched} matching this protocol, {review} needing a human "
        f"check, and {no_match} not matching."
    )


def verdict(results: Sequence[Mapping[str, Any]]) -> tuple[bool, bool]:
    """`(eligible, needs_review)` from a patient's criterion results.

    The rule, once: eligibility is unanimity among the criteria that could be
    decided, and any criterion that could *not* be decided sends the patient to a
    human regardless. A patient with nothing decidable is not eligible — `bool(known)`
    is what keeps a cohort of all-unknown verdicts from reading as a perfect match.

    Public because the what-if simulator (#95) re-derives both flags after moving a
    threshold, and a simulated cohort bucketed by a second copy of this rule would
    eventually disagree with the run it is simulating.
    """
    known = [r for r in results if r["status"] != "unknown"]
    eligible = bool(known) and all(r["status"] == "pass" for r in known)
    return eligible, any(r["status"] == "unknown" for r in results)


def evaluate_patient(patient: dict, criteria: dict, verdicts: dict | None = None) -> dict:
    """Score one patient against one extraction.

    Quantitative results carry `observed` — the lab value the comparison was made
    against, or None when the record had none. It is what makes a verdict
    re-derivable from the checkpoint alone: the what-if simulator (#95) moves a
    threshold and re-applies it to `observed` rather than re-reading the EHR, so a
    simulation touches no patient data beyond what this run already evaluated, and
    a run checkpointed months ago simulates from its own numbers. Nothing new is
    exposed by recording it — the explanation beside it already prints the value in
    words ("The patient's eGFR is 42 mL/min/1.73m2").
    """
    verdicts = verdicts or {}
    results = []
    for c in criteria["inclusion_quantitative"]:
        observed = patient["labs"].get(c["attribute"])
        status = compare_quantitative(observed, c)
        results.append(
            {
                "criterion": c,
                "kind": "inclusion",
                "status": status,
                "observed": observed,
                "explanation": _explain_quantitative(observed, c, "inclusion", status),
            }
        )
    for c in criteria["inclusion_categorical"]:
        presence, evidence = _categorical_presence(patient, c, verdicts)
        status = _inclusion_status(presence, c["negated"])
        results.append(
            {
                "criterion": c,
                "kind": "inclusion",
                "status": status,
                "explanation": _explain_categorical(c, "inclusion", presence, evidence),
            }
        )
    # A patient MATCHING an exclusion criterion fails screening
    for c in criteria["exclusion_quantitative"]:
        observed = patient["labs"].get(c["attribute"])
        flipped = flip_exclusion(compare_quantitative(observed, c))
        results.append(
            {
                "criterion": c,
                "kind": "exclusion",
                "status": flipped,
                "observed": observed,
                "explanation": _explain_quantitative(observed, c, "exclusion", flipped),
            }
        )
    # Presence of an excluded term fails the patient. We match on presence and
    # ignore the criterion's `negated` flag on purpose: the exclusion list
    # already carries the "must not have" meaning, so also honoring `negated`
    # here would double-negate — wrongly failing every patient who LACKS the
    # excluded condition whenever the parser sets negated=True on an exclusion.
    for c in criteria["exclusion_categorical"]:
        presence, evidence = _categorical_presence(patient, c, verdicts)
        if presence == "uncertain":
            status = "unknown"
        else:
            status = "fail" if presence == "present" else "pass"
        results.append(
            {
                "criterion": c,
                "kind": "exclusion",
                "status": status,
                "explanation": _explain_categorical(c, "exclusion", presence, evidence),
            }
        )

    eligible, needs_review = verdict(results)
    return {
        "patient_id": patient["id"],
        "name": patient.get("name"),
        "eligible": eligible,
        "needs_review": needs_review,
        "criterion_results": results,
        "summary": summarize_patient(
            patient.get("name") or patient["id"], results, eligible, needs_review
        ),
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


class TermMappingCost(NamedTuple):
    """What the term-mapping step cost the cohort, and what it would have cost
    per patient (#101).

    `llm_pairs` is the distinct `(criterion, term)` pairs actually sent to a
    model — the cache's misses, and the thing the LLM was billed for.
    `resolutions` is how many of those pairs the cohort's patients *between them*
    need resolved: the same pair recurs once per patient carrying that term, and
    a matcher that resolved per patient would have asked once for each.

    So `1 - llm_pairs / resolutions` is the term-mapping cache hit rate — the
    caching claim as a number, computed where both halves are already in hand.
    It counts a pair once per patient that carries the term rather than once per
    mention, because a patient listing a drug twice is still one patient's
    question. It is an upper bound on the lookups `_categorical_presence`
    actually performs, which stops early on the first match: the figure is what
    the cohort's terms *require*, which is the honest denominator for "how much
    did caching save".
    """

    resolutions: int
    llm_pairs: int


def _fetch_from_store(
    store: TermStore, pairs: Sequence[tuple[str, str]], model_id: str
) -> dict[tuple[str, str], str]:
    if not pairs:
        return {}
    if hasattr(store, "get_cached"):
        return store.get_cached(pairs, model_id)
    cached = store.get_cached(pairs, model_id)
    if cached:
        return cached
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None and loop.is_running():
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, store.get_many(pairs, model_id)).result()
    return asyncio.run(store.get_many(pairs, model_id))


def _save_to_store(store: TermStore, records: Sequence[TermRecord]) -> None:
    if not records:
        return
    if hasattr(store, "set_cached"):
        store.set_cached(records)
        return
    store.set_cached(records)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None and loop.is_running():
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(asyncio.run, store.set_many(records)).result()
            return
    asyncio.run(store.set_many(records))


def build_verdict_cache(
    criteria: dict,
    patients: list[dict],
    mapper: TermMapper = _map_terms_via_llm,
    on_progress: Callable[[int, int], None] | None = None,
    on_cost: Callable[[TermMappingCost], None] | None = None,
    store: TermStore | None = None,
    model_id: str | None = None,
) -> dict[tuple[str, str], str]:
    """Resolve the ambiguous categorical tail once for the whole cohort.

    For each distinct categorical criterion, gather every patient term the fast
    path can't already settle, check Tier 1 (per-screening) and Tier 2 (durable
    cross-run store, keyed by criterion_value, patient_term, model_id) caches,
    ask the mapper in one batch for cache misses, and persist the results.
    An unavailable LLM degrades to "uncertain" for that criterion's terms → the
    affected patients land in needs-review rather than being silently passed or failed.

    `on_progress(done, total)` is called before each LLM mapper call. Each call
    can take tens of seconds on a local model, so this lets the caller emit a
    keepalive/progress signal between them (see matcher_node).

    `on_cost(TermMappingCost)` is called once, at the end, with what the cache
    saved (#101, #105) — see `TermMappingCost`.
    """
    categoricals = criteria["inclusion_categorical"] + criteria["exclusion_categorical"]
    # One representative original spelling per normalized term, for the prompt.
    term_by_norm: dict[str, str] = {}
    # How many patients carry each normalized term — the multiplier a per-patient
    # implementation would have paid on every pair. Counted per patient, not per
    # mention: `patient_terms` concatenates three fields and one patient can list
    # the same drug in two of them.
    patients_with_term: dict[str, int] = {}
    for p in patients:
        seen: set[str] = set()
        for t in patient_terms(p):
            tnorm = _norm(t)
            term_by_norm.setdefault(tnorm, t)
            if tnorm not in seen:
                seen.add(tnorm)
                patients_with_term[tnorm] = patients_with_term.get(tnorm, 0) + 1

    cache: dict[tuple[str, str], str] = {}
    active_store = store if store is not None else terms_service.current_term_store()
    active_model = model_id or terms_service.active_model_id()

    # Preload from durable cross-run storage (Tier 2) for all unsettled pairs (#105)
    all_unsettled_pairs: list[tuple[str, str]] = []
    for c in categoricals:
        cval = _norm(c["value"])
        for tnorm in term_by_norm:
            if not fast_present(cval, tnorm):
                all_unsettled_pairs.append((cval, tnorm))

    if active_store is not None and all_unsettled_pairs:
        try:
            cached_durable = _fetch_from_store(active_store, all_unsettled_pairs, active_model)
            cache.update(cached_durable)
        except Exception as exc:  # noqa: BLE001
            # AC 5: Store outage gracefully degrades to Tier 1 in-process behavior
            log.warning("matcher.term_cache_read_failed", error=type(exc).__name__, detail=str(exc))

    resolutions = 0
    newly_resolved_records: list[TermRecord] = []
    total_llm_pairs = 0

    # Only criteria with an ambiguous tail that is not already cached make an LLM call;
    # count those for a meaningful progress denominator.
    llm_bound = [
        c
        for c in categoricals
        if any(
            not fast_present(_norm(c["value"]), tnorm) and (_norm(c["value"]), tnorm) not in cache
            for tnorm in term_by_norm
        )
    ]
    done = 0
    now_stamp = datetime.now(UTC).isoformat()
    for c in categoricals:
        cval = _norm(c["value"])
        # One `fast_present` sweep per criterion, reused for both the candidate
        # set and the cost figure: it is a regex scan per (criterion, term) pair,
        # and on a large cohort a second sweep for accounting alone would be tens
        # of thousands of extra scans per screening.
        unsettled = [tnorm for tnorm in term_by_norm if not fast_present(cval, tnorm)]
        # Counted before the `continue` below: a criterion whose terms are all
        # already cached (the protocol quotes it twice, or durable cache hit) still
        # had those resolutions required by the cohort — they were simply served
        # from the cache, which is precisely the saving being measured.
        resolutions += sum(patients_with_term.get(tnorm, 0) for tnorm in unsettled)
        candidates = {
            tnorm: term_by_norm[tnorm] for tnorm in unsettled if (cval, tnorm) not in cache
        }
        if not candidates:
            continue
        if on_progress is not None:
            on_progress(done, len(llm_bound))
        done += 1
        total_llm_pairs += len(candidates)
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
            verdict = verdicts.get(tnorm, "no_match")
            cache[(cval, tnorm)] = verdict
            newly_resolved_records.append(
                TermRecord(
                    criterion_value=cval,
                    patient_term=tnorm,
                    model_id=active_model,
                    verdict=verdict,
                    created_at=now_stamp,
                )
            )

    if active_store is not None and newly_resolved_records:
        try:
            _save_to_store(active_store, newly_resolved_records)
        except Exception as exc:  # noqa: BLE001
            # AC 5: Store outage on write does not fail the run
            log.warning(
                "matcher.term_cache_write_failed", error=type(exc).__name__, detail=str(exc)
            )

    if on_cost is not None:
        on_cost(TermMappingCost(resolutions=resolutions, llm_pairs=total_llm_pairs))
    return cache


def cohort_terms(patients: list[dict]) -> list[str]:
    """Every normalized term the cohort put in front of the term mapper.

    Sorted for a stable checkpoint: an unordered set would re-serialize
    differently on every run and turn a diff of two checkpoints into noise.
    """
    return sorted({_norm(t) for p in patients for t in patient_terms(p)})


def serialize_verdicts(cache: dict[tuple[str, str], str], terms: list[str]) -> dict:
    """The verdict cache in a form a checkpoint can hold — and a later reader trust.

    `{"terms": [...], "verdicts": [[criterion_value, term, verdict], ...]}`, all
    normalized, tuple keys flattened because JSON has no tuple.

    **Only non-"no_match" verdicts are kept**, which is lossless *given* `terms`.
    `_categorical_presence` treats a cached "no_match" and a cache miss
    identically — neither contributes presence — so a reader that knows which
    terms were offered can tell the two apart where it matters and needs the
    entry nowhere else. What `terms` buys is the distinction between "the mapper
    was asked about this term and said no" and "this term was never put to it":
    the first is an answer, the second is a gap, and a reverse match has to send
    the second to a human rather than quietly read it as absence (#96).

    The filter is not micro-optimization. "no_match" is the overwhelming majority
    verdict — every criterion is asked about every unsettled term in the cohort,
    and a protocol's six categorical criteria against two hundred distinct terms
    is twelve hundred pairs of which a handful match. Storing them all would add
    a five-figure JSON blob to every checkpoint to record, over and over, the
    default.
    """
    return {
        "terms": terms,
        "verdicts": sorted(
            [cval, term, verdict]
            for (cval, term), verdict in cache.items()
            if verdict != "no_match"
        ),
    }


def load_patients() -> list[dict]:
    """Read the synthetic EHR; a missing or corrupt file is a DataStoreError.

    Raised (not absorbed into state) on purpose: it fires at the very start of
    the matcher, before any state advances, so the checkpoint stays parked at
    the gate and approval is retryable once the store is fixed. Approval now
    streams (see services.screening.approve_screening), so this surfaces as a
    terminal __error__ frame on the approve SSE stream rather than a 503.
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


def _progress_emitter() -> Callable[[int, int], None]:
    """A callback that pushes matcher progress onto the graph's custom stream.

    `get_stream_writer()` only works inside a streaming graph run; called
    anywhere else (direct unit tests, a non-streaming invoke) it raises, so we
    degrade to a no-op. Emitted events reach the approve SSE stream as
    `__progress__` frames — see services.screening.approve_screening.
    """
    try:
        writer = get_stream_writer()
    except RuntimeError:
        return lambda _done, _total: None

    def emit(done: int, total: int) -> None:
        writer({"phase": "matching", "done": done, "total": total})

    return emit


def matcher_node(state: ScreenerState) -> dict:
    criteria = state["parsed_criteria"]
    assert criteria is not None, "matcher runs after parser — parsed_criteria is set"
    patients = load_patients()
    verdicts = build_verdict_cache(
        criteria,
        patients,
        on_progress=_progress_emitter(),
        # The caching claim, counted (#101): what the cohort's terms required
        # against what the LLM was actually asked. Recorded here rather than
        # inside `build_verdict_cache` so that function stays a pure derivation
        # its unit tests can call without touching the metrics registry.
        on_cost=lambda cost: metrics.record_term_mapping(cost.resolutions, cost.llm_pairs),
    )
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
        "match_summary": summarize_cohort(evaluations),
        # What the ambiguous tail resolved to, kept so a later reader can score a
        # patient this run never saw without asking a model again (#96).
        "term_mappings": serialize_verdicts(verdicts, cohort_terms(patients)),
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
