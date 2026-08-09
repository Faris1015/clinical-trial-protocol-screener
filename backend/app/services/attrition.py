"""Per-criterion cohort attrition (#94) — what is killing the cohort.

The app could already answer *who* is eligible. The question a coordinator
actually asks is *what is screening my cohort out*: "eGFR ≥ 60 excludes 41 of
100, ECOG ≤ 1 excludes 28, and 19 patients fail both" is the difference between
a verdict and a decision — relax the renal bound, or go find a different site.

Everything that answer needs is already in the checkpoint. The Matcher writes a
per-criterion `status` for every patient into `matched_patients`, so this module
is a pure reduction over data the run has already paid for: no LLM call, no extra
read, and the same numbers for a run checkpointed months ago as for one that
finished a second ago.

Four decisions worth knowing before editing this module:

**The buckets come from `services/cohort.py`, not from here.** The cohort split is
already rendered in four places and the rule for it lives in one module; a fifth
rendering that recomputed "eligible" from criterion statuses would eventually
disagree with the other four over some edge case, and a screen where the tally
contradicts the table below it is worse than no tally. `totals` calls
`cohort.bucket_counts` and reports what it returns.

**A criterion's counts are patient counts, not row counts.** Each key is counted
once per patient even if the extraction contains the criterion twice, so
`excluded + unresolved + passed` is always the number of patients the criterion
was applied to. Within one patient the worst status wins: a criterion that failed
someone excluded them, whatever a duplicate of it did.

**`unique` is the honest delta.** Reporting only "eGFR excludes 41" invites the
reader to believe relaxing it returns 41 patients, when 19 of them also fail
ECOG. So every criterion carries how many of its exclusions are *unique* to it
versus shared with another criterion, and `recoverable` — how many patients would
actually land in the eligible bucket if this one criterion were dropped, which is
`unique` minus those who would still need a human because something else about
them could not be evaluated. That is the number a coordinator acts on, and it is
the data model the what-if simulator (#95) drives.

**Ranked, and deterministic.** Most exclusions first, ties broken by unresolved
count and then by label — so two exports of one run put the criteria in the same
order, and the ranking never depends on dict iteration order.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from itertools import combinations
from typing import Any, NamedTuple, TypedDict

from app.services import cohort
from app.services.criteria_edits import criterion_label

# How many of the ranked criteria get pairwise overlap reported. Every pair of the
# top five is ten figures at most, which is a readable block; the whole matrix of
# a twenty-criterion protocol is 190 and answers a question nobody asked. The
# overlap that matters is between the criteria a reader is considering relaxing,
# and those are the ones at the top.
OVERLAP_DEPTH = 5

# Worst-wins ordering when one key resolves to several statuses for one patient
# (a duplicated criterion in the extraction). Higher is worse.
_SEVERITY = {"pass": 0, "unknown": 1, "fail": 2}


class CriterionAttrition(TypedDict):
    """What one criterion did to the cohort.

    `excluded` is patients it failed, `unresolved` patients it could not be
    evaluated for (a missing lab, an ambiguous term the LLM would not settle) and
    `passed` the rest — the three partition the patients the criterion was applied
    to, which is normally the whole cohort.

    `unique` is the part of `excluded` no other criterion also failed, `shared` the
    remainder, and `recoverable` the patients who would end up *eligible* if this
    criterion were dropped — smaller than `unique` whenever one of those patients
    has an unresolved criterion that would still send them to a human.

    `share` is `excluded` as a percentage of the cohort, rounded to one decimal by
    the API so the figure a reviewer reads and the bar they compare it against are
    the same value rather than two roundings of it.
    """

    key: str
    label: str
    kind: str
    source_text: str
    excluded: int
    unresolved: int
    passed: int
    unique: int
    shared: int
    recoverable: int
    share: float


class CriterionOverlap(TypedDict):
    """How many patients two criteria both exclude.

    Directionless — one entry per pair, not two — and only pairs that actually
    overlap are reported: a list of zeros says nothing a reader could act on.
    """

    a_key: str
    b_key: str
    a_label: str
    b_label: str
    patients: int


class AttritionTotals(TypedDict):
    """The cohort in figures, above the per-criterion rows.

    `eligible`/`review`/`ineligible` are `cohort.bucket_counts` verbatim — the same
    three numbers the cohort table, the runs index and the report print. The other
    three are this module's own decomposition of them:

    `excluded` is patients at least one criterion failed and `unresolved` patients
    at least one criterion could not be evaluated for; the two overlap, because a
    patient can both fail one criterion and be indeterminate on another.

    `unscored` is patients no criterion was applied to at all — an extraction with
    no structured criteria in it. Whichever bucket they landed in, no criterion row
    below accounts for them, and naming them here is what keeps the rows
    reconciling with the buckets above instead of quietly falling short.
    """

    patients: int
    eligible: int
    review: int
    ineligible: int
    excluded: int
    unresolved: int
    unscored: int


class CohortAttrition(TypedDict):
    """The full breakdown: the totals, the ranked criteria, and the top overlaps.

    `criteria` holds every criterion the run applied, including those that excluded
    nobody — "age ≥ 18 excluded 0" is a fact about the protocol, and a list that
    silently dropped the harmless criteria would leave a reader unable to tell a
    criterion that passed everyone from one the Matcher never applied.
    """

    totals: AttritionTotals
    criteria: list[CriterionAttrition]
    overlaps: list[CriterionOverlap]


def _mapping(item: Any) -> Mapping[str, Any]:
    """One entry of the checkpoint, defensively.

    Every input here comes from a checkpoint that may have been written by an
    older build of the pipeline or by a run that failed partway, so a wrongly
    typed entry degrades to an empty section rather than raising on a page load.
    """
    return item if isinstance(item, Mapping) else {}


def _items(values: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    """A list-valued field, guarded the same way — a string is not a list of rows."""
    value = values.get(key)
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return []
    return [_mapping(item) for item in value]


def _status(result: Mapping[str, Any]) -> str:
    """The result's status, normalized to one of the three the Matcher writes.

    Anything else — a status from a future build, a null — is read as `unknown`:
    an unrecognized verdict is exactly a criterion this module cannot evaluate,
    and counting it as a pass would quietly inflate the eligible side.
    """
    status = str(result.get("status") or "")
    return status if status in _SEVERITY else "unknown"


def _key(kind: str, label: str) -> str:
    """A criterion's identity across patients.

    Kind plus rendered label, not list position: position is stable within one
    run but says nothing to a reader, and the label is the same string the report
    and the edit history name the criterion with (`criteria_edits.criterion_label`)
    — a reviewer comparing the two must not see one criterion written two ways.

    Two extracted criteria that render identically therefore merge into one row.
    That is the intended reading: they are the same requirement, quoted twice.
    """
    return f"{kind}:{label}"


class _Applied(NamedTuple):
    """One criterion as it was applied to one patient."""

    status: str
    kind: str
    label: str
    source_text: str


def _patient_criteria(evaluation: Mapping[str, Any]) -> dict[str, _Applied]:
    """Every criterion applied to this patient, keyed, worst status winning.

    Collapsing to a dict is what makes the counts below patient counts: a
    criterion the extraction contains twice contributes one entry, so it cannot
    count the same patient twice in its own `excluded` figure.
    """
    applied: dict[str, _Applied] = {}
    for result in _items(evaluation, "criterion_results"):
        criterion = _mapping(result.get("criterion"))
        if not criterion:
            # A result row carrying no criterion — a null in a hand-edited
            # checkpoint. There is nothing to attribute it to, and inventing a row
            # labelled " ()" would put a phantom criterion at the top of a ranking.
            continue
        kind = str(result.get("kind") or "")
        label = criterion_label(criterion)
        key = _key(kind, label)
        entry = _Applied(
            status=_status(result),
            kind=kind,
            label=label,
            source_text=str(criterion.get("source_text") or ""),
        )
        previous = applied.get(key)
        if previous is None or _SEVERITY[entry.status] > _SEVERITY[previous.status]:
            applied[key] = entry
    return applied


class _Tally:
    """Mutable accumulator for one criterion, before it becomes a TypedDict."""

    def __init__(self, kind: str, label: str, source_text: str) -> None:
        self.kind = kind
        self.label = label
        # The first provenance seen for this key. Merged duplicates keep one
        # sentence rather than concatenating two, and a criterion carrying no
        # provenance shows none instead of the string "None".
        self.source_text = source_text
        self.excluded = 0
        self.unresolved = 0
        self.passed = 0
        self.unique = 0
        self.recoverable = 0


def build_attrition(values: Mapping[str, Any]) -> CohortAttrition:
    """Reduce a run's evaluated cohort to per-criterion attrition.

    `values` is the checkpoint block of `GET /api/screenings/{id}/state` — the same
    argument `timeline.build_timeline` takes, so a caller holding one payload can
    derive both without unpacking either. Only `matched_patients` is read.

    A run that never reached the Matcher has no cohort, and gets an all-zero
    breakdown with no rows — the callers render nothing at all for that rather than
    a table of zeros, but the shape is still complete so neither has to
    special-case a null.
    """
    evaluations = _items(values, "matched_patients")
    buckets = cohort.bucket_counts(evaluations)

    tallies: dict[str, _Tally] = {}
    # Patient id sets per criterion, kept only for the pairwise overlap below:
    # "how many patients fail both of these" cannot be recovered from counts.
    # Keyed by index rather than by `patient_id`, which a hand-edited checkpoint
    # could repeat — two patients sharing an id would collapse into one here and
    # under-report the overlap.
    excluded_by: dict[str, set[int]] = {}
    excluded_patients = 0
    unresolved_patients = 0
    unscored = 0

    for index, evaluation in enumerate(evaluations):
        applied = _patient_criteria(evaluation)
        if not applied:
            unscored += 1
            continue

        failed = {key for key, entry in applied.items() if entry.status == "fail"}
        unresolved = {key for key, entry in applied.items() if entry.status == "unknown"}
        if failed:
            excluded_patients += 1
        if unresolved:
            unresolved_patients += 1

        for key, entry in applied.items():
            status = entry.status
            tally = tallies.setdefault(key, _Tally(entry.kind, entry.label, entry.source_text))
            if status == "fail":
                tally.excluded += 1
                excluded_by.setdefault(key, set()).add(index)
                if len(failed) == 1:
                    tally.unique += 1
                    # Dropping this criterion clears the patient's last failure.
                    # They only reach the eligible bucket if nothing else about
                    # them was indeterminate — otherwise they move from
                    # "ineligible" to "needs review", which is progress but not
                    # a match, and promising it as one is the false delta this
                    # figure exists to prevent.
                    if not unresolved:
                        tally.recoverable += 1
            elif status == "unknown":
                tally.unresolved += 1
            else:
                tally.passed += 1

    total = len(evaluations)
    criteria = sorted(
        (
            CriterionAttrition(
                key=key,
                label=tally.label,
                kind=tally.kind,
                source_text=tally.source_text,
                excluded=tally.excluded,
                unresolved=tally.unresolved,
                passed=tally.passed,
                unique=tally.unique,
                shared=tally.excluded - tally.unique,
                recoverable=tally.recoverable,
                share=round(tally.excluded * 100 / total, 1) if total else 0.0,
            )
            for key, tally in tallies.items()
        ),
        # Most restrictive first. Unresolved breaks the tie because a criterion
        # the Matcher could not evaluate is the next thing a coordinator has to
        # deal with, and the label breaks the remaining ties so the order is
        # reproducible rather than insertion-dependent.
        key=lambda row: (-row["excluded"], -row["unresolved"], row["label"]),
    )

    return CohortAttrition(
        totals=AttritionTotals(
            patients=total,
            eligible=buckets["eligible"],
            review=buckets["review"],
            ineligible=buckets["ineligible"],
            excluded=excluded_patients,
            unresolved=unresolved_patients,
            unscored=unscored,
        ),
        criteria=criteria,
        overlaps=_overlaps(criteria, excluded_by),
    )


def _overlaps(
    criteria: Sequence[CriterionAttrition], excluded_by: Mapping[str, set[int]]
) -> list[CriterionOverlap]:
    """Pairwise exclusion overlap among the top `OVERLAP_DEPTH` criteria.

    Ordered by size, then by the pair's labels so equal overlaps come out in a
    fixed order. A pair that shares no patients is omitted rather than reported as
    zero: the reader is looking for the double-counting that would make a naive
    sum of the rows above wrong, and only non-empty intersections do that.
    """
    top = [row["key"] for row in criteria[:OVERLAP_DEPTH] if row["excluded"]]
    labels = {row["key"]: row["label"] for row in criteria}
    overlaps = [
        CriterionOverlap(
            a_key=a,
            b_key=b,
            a_label=labels[a],
            b_label=labels[b],
            patients=len(excluded_by.get(a, set()) & excluded_by.get(b, set())),
        )
        for a, b in combinations(top, 2)
    ]
    return sorted(
        (overlap for overlap in overlaps if overlap["patients"]),
        key=lambda overlap: (-overlap["patients"], overlap["a_label"], overlap["b_label"]),
    )
