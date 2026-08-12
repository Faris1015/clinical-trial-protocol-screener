"""Reverse matching (#96) — one patient against every trial, not one trial
against every patient.

The pipeline runs in one direction: a protocol arrives, the Matcher scores the
cohort, and the answer is a table of patients. The question a coordinator
actually holds a patient in their hand for is the transpose — *`PT-0001` is in
front of me; what are they eligible for?* — and it is the same deterministic
Matcher, iterated over stored runs instead of over patients.

**Two ways a verdict gets here, and the difference matters.**

*Recorded.* The patient is in that run's `matched_patients`, so the run already
answered. The recorded evaluation is returned verbatim — statuses, explanations,
summary and all — and bucketed through `services/cohort.py`. This is not an
optimization, it is the correctness argument for AC 4: a verdict *read from* the
run cannot disagree with the run's own cohort table, whereas one recomputed
beside it eventually would. Almost every row is this one, because every run
scores the same EHR.

*Rematched.* The patient is not in the run's cohort — the records were
regenerated, or the run predates them, or its Matcher never finished. Here the
patient really is scored, by `matcher.evaluate_patient`, against the criteria the
run was approved with.

**A rematch makes no LLM call and never re-runs the Critic.** The quantitative
half is arithmetic. The categorical half needs the ambiguous-tail verdicts the
run resolved, and it gets them from the run's own checkpoint (#96 added
`term_mappings` for exactly this). The Critic does not enter into it at all: its
findings are about the *protocol*, they were reached when the run was approved,
and re-deriving them for a patient view would be re-litigating a decision a human
already made.

**What the mappings cannot answer goes to a human.** A patient may carry a term
the run never saw, so no cached verdict exists and the fast path cannot settle it
— "prior pemetrexed" against a criterion of "prior platinum chemotherapy", say.
That pair is forced to *uncertain*, which puts the patient in needs-review, and
the count is reported as `unmapped` so the reader knows the verdict is short of
an answer rather than being one. Reading an unasked question as absence is the
one failure mode here that would be invisible: it produces a confident
"ineligible" with nothing to show that it was a guess.

Runs scored before `term_mappings` existed have none, so *every* ambiguous pair
is unmapped and such a patient lands in needs-review with a large `unmapped`.
That is the honest reading of a checkpoint that did not record its own reasoning,
and it degrades exactly as an unavailable LLM does in the Matcher itself.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, TypedDict

from app.graph.nodes import matcher
from app.services import cohort
from app.services.checkpoint import rows

# The verdict a pair gets when the run's mappings cannot speak to it. The same
# value `build_verdict_cache` falls back to when the LLM is unreachable, and it
# reaches `_categorical_presence` through the same cache argument — so an
# unanswerable pair takes the path the Matcher already has for one, rather than a
# second notion of "undecided" living out here.
UNMAPPED = "uncertain"


class TrialMatch(TypedDict):
    """One trial's answer for one patient.

    `bucket` is `services/cohort.py`'s, from the same `(eligible, needs_review)`
    pair the cohort table buckets on — so a recorded verdict here reads
    identically to the row it came from.

    `source` is `"recorded"` or `"rematched"`: whether the run itself scored this
    patient or this module did. Surfaced rather than smoothed over because the two
    have different standing — the first is the run's own answer, the second is
    what its criteria would say — and a reader deciding whether to act on a match
    is entitled to know which they are looking at.

    `unmapped` is how many categorical criteria a rematch could not settle from
    the run's stored mappings; always 0 for a recorded verdict.
    """

    thread_id: str
    source_filename: str
    trial_title: str
    status: str
    created_at: str
    bucket: str
    eligible: bool
    needs_review: bool
    summary: str
    criterion_results: list[dict[str, Any]]
    source: str
    unmapped: int


class ReverseMatch(TypedDict):
    """Every trial this patient was put to, worst-last by bucket.

    `counts` is `cohort.bucket_counts` over the trials — the same three-bucket
    reduction the cohort table shows, transposed. `scanned`/`total` state the
    window: this walks the most recent runs, and a page claiming "2 eligible
    trials" without saying it looked at thirty of forty would be understating by
    an amount the reader cannot see.
    """

    patient_id: str
    trials: list[TrialMatch]
    counts: dict[str, int]
    scanned: int
    total: int


def recover_verdicts(values: Mapping[str, Any]) -> tuple[dict[tuple[str, str], str], set[str]]:
    """The run's term mappings, back in the form `evaluate_patient` takes.

    Returns `(cache, asked)` — the verdict per `(criterion_value, term)` and the
    set of terms the run put to the mapper at all. Both normalized, as the Matcher
    normalizes.

    `asked` is the half that keeps this honest, and it is why `serialize_verdicts`
    stores a term list beside the verdicts. A pair that is absent from `cache` is
    either "the mapper said no_match" (dropped, because it is the default) or
    "nobody ever asked" — and those must not be read the same way. Membership of
    `asked` is what separates them.

    Anything malformed degrades to empty rather than raising: this runs on a
    read-only page over a checkpoint that may have been written by an older build,
    and an empty cache means "ask a human", which is the safe direction.
    """
    mappings = values.get("term_mappings")
    if not isinstance(mappings, Mapping):
        return {}, set()

    raw_terms = mappings.get("terms")
    asked: set[str] = set()
    if isinstance(raw_terms, Sequence) and not isinstance(raw_terms, str | bytes):
        asked = {t for t in raw_terms if isinstance(t, str)}

    cache: dict[tuple[str, str], str] = {}
    raw_verdicts = mappings.get("verdicts")
    if isinstance(raw_verdicts, Sequence) and not isinstance(raw_verdicts, str | bytes):
        for entry in raw_verdicts:
            # Each entry is a `[criterion, term, verdict]` triple; a row of any
            # other shape is skipped rather than unpacked, since a ValueError here
            # would 500 a page over one bad row in someone else's checkpoint.
            if not isinstance(entry, Sequence) or isinstance(entry, str | bytes):
                continue
            if len(entry) != 3 or not all(isinstance(part, str) for part in entry):
                continue
            criterion_value, term, verdict = entry
            cache[(criterion_value, term)] = verdict
            # A verdict is itself proof the pair was asked, so a checkpoint whose
            # `terms` list is missing or truncated still resolves everything it
            # recorded an answer for.
            asked.add(term)
    return cache, asked


def _categorical_values(criteria: Mapping[str, Any]) -> list[str]:
    """Every categorical criterion's value, normalized — the questions this run's
    criteria ask of a patient's terms."""
    values: list[str] = []
    for bucket in ("inclusion_categorical", "exclusion_categorical"):
        for criterion in rows(criteria, bucket):
            value = criterion.get("value")
            if isinstance(value, str):
                values.append(value.strip().lower())
    return values


def _fill_unmapped(
    patient: Mapping[str, Any],
    criteria: Mapping[str, Any],
    cache: dict[tuple[str, str], str],
    asked: set[str],
) -> int:
    """Force every pair the run's mappings cannot speak to to `UNMAPPED`.

    Mutates `cache` — it is this call's freshly recovered copy, never the
    checkpoint — and returns how many *criteria* ended up with at least one such
    pair. Counted per criterion rather than per pair because that is what a reader
    can act on: "2 criteria could not be checked" names a gap they can go and look
    at, while "37 term pairs" is an implementation detail of how the check works.

    A pair is left alone when the fast path settles it (a word-boundary hit needs
    no mapping) or when the run recorded an answer for that term. What remains is
    a question that was never put to anything, and absence of an answer is not an
    answer of absence.
    """
    terms = [t.strip().lower() for t in matcher.patient_terms(dict(patient))]
    unmapped = 0
    for cval in _categorical_values(criteria):
        gap = False
        for term in terms:
            if matcher.fast_present(cval, term) or term in asked:
                continue
            cache[(cval, term)] = UNMAPPED
            gap = True
        unmapped += gap
    return unmapped


def _recorded(
    evaluations: Sequence[Mapping[str, Any]], patient_id: str
) -> Mapping[str, Any] | None:
    for evaluation in evaluations:
        if evaluation.get("patient_id") == patient_id:
            return evaluation
    return None


def _trial_title(criteria: Mapping[str, Any], source_filename: str) -> str:
    """What to call this trial. The extraction's own title where the Parser found
    one, the uploaded filename otherwise — a row headed by neither would be a link
    with nothing on it."""
    title = criteria.get("trial_title")
    return title if isinstance(title, str) and title.strip() else source_filename


def match_run(
    patient: Mapping[str, Any],
    values: Mapping[str, Any],
    *,
    thread_id: str,
    source_filename: str,
    status: str,
    created_at: str,
) -> TrialMatch | None:
    """This patient's verdict for one run, or None when the run cannot give one.

    None means the run never reached approved criteria — no `parsed_criteria`, or
    no approval on the checkpoint. Both are runs whose criteria no human has
    cleared, and answering from them would put a verdict in front of a coordinator
    that the gate exists to prevent existing. Skipped rather than reported as
    "ineligible", which would be a claim rather than a silence.

    The caller filters its walk to finished runs so the budget is not spent on
    checkpoints that end up here; this check stays anyway, and is the authority.
    A run's *status* is denormalized onto the store row when it reaches a terminal
    frame, whereas `approved_at` is written into the checkpoint by the gate itself
    — so this is the one that cannot be stale, and it is the one that means what
    is actually being asked.
    """
    criteria = values.get("parsed_criteria")
    if not isinstance(criteria, Mapping) or not values.get("approved_at"):
        return None

    patient_id = str(patient.get("id") or "")
    evaluation = _recorded(rows(values, "matched_patients"), patient_id)
    if evaluation is not None:
        source, unmapped = "recorded", 0
        eligible = bool(evaluation.get("eligible"))
        needs_review = bool(evaluation.get("needs_review"))
        summary = str(evaluation.get("summary") or "")
        results = [dict(r) for r in rows(evaluation, "criterion_results")]
    else:
        cache, asked = recover_verdicts(values)
        unmapped = _fill_unmapped(patient, criteria, cache, asked)
        # The Matcher's own scoring function, not a copy of it: the criteria are
        # the run's, the cache is the run's, and the only thing this module
        # supplies is the patient.
        scored = matcher.evaluate_patient(dict(patient), dict(criteria), cache)
        source = "rematched"
        eligible = scored["eligible"]
        needs_review = scored["needs_review"]
        summary = scored["summary"]
        results = scored["criterion_results"]

    return TrialMatch(
        thread_id=thread_id,
        source_filename=source_filename,
        trial_title=_trial_title(criteria, source_filename),
        status=status,
        created_at=created_at,
        # One rule, read from `services/cohort.py`, over the same two flags the
        # run's own table buckets on.
        bucket=cohort.bucket_of({"eligible": eligible, "needs_review": needs_review}),
        eligible=eligible,
        needs_review=needs_review,
        summary=summary,
        criterion_results=results,
        source=source,
        unmapped=unmapped,
    )


def build_reverse_match(
    patient: Mapping[str, Any], trials: Sequence[TrialMatch], *, scanned: int, total: int
) -> ReverseMatch:
    """Order the trials and count them — the shape the patient view renders.

    Sorted by bucket in `cohort.BUCKET_ORDER` (best first), then newest run first
    inside a bucket. The eligible trials are the answer to the question that was
    asked; the ineligible ones are context, and a page that made a coordinator
    scroll past thirty of them to find a match would be answering a different one.
    """
    order = {bucket: i for i, bucket in enumerate(cohort.BUCKET_ORDER)}
    # Two passes over Python's stable sort rather than one key that has to invert
    # a timestamp: the two orderings run in opposite directions, and a single
    # `reverse=True` would flip the buckets along with the dates. `created_at` is
    # an ISO-8601 UTC instant, so descending string order is descending time
    # order, and a run without one sorts last — where an unknown belongs.
    ranked = sorted(trials, key=lambda t: t["created_at"], reverse=True)
    # `len(order)` parks a bucket this module does not know at the end rather than
    # raising, the same way `cohort.bucket_counts` counts one rather than dropping it.
    ranked.sort(key=lambda t: order.get(t["bucket"], len(order)))
    return ReverseMatch(
        patient_id=str(patient.get("id") or ""),
        trials=ranked,
        counts=cohort.bucket_counts(ranked),
        scanned=scanned,
        total=total,
    )
