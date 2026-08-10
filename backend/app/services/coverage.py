"""Screenability (#93) — how much of a protocol this run could actually check.

`EhrAttribute` is a closed vocabulary of eleven attributes, so a sentence the
Parser cannot convert lands in `unparseable`; a criterion the Matcher cannot
settle for a patient comes back `unknown`. Both are handled honestly at the
criterion level, and both were invisible above it — a protocol where 6 of 20
criteria never parsed screened "successfully" and looked identical in the runs
index to one where all 20 did.

This module is the number that was missing: *how much of this protocol we could
actually check*. It is the figure a reviewer needs before they approve a run, and
aggregated over runs it is also the vocabulary backlog — the phrasings that cost
the most coverage are the next `EhrAttribute` worth adding.

Four decisions worth knowing before editing this module:

**Two layers, one score.** A criterion is checkable only if it was *structured*
(the Parser turned it into a criterion) **and** *resolved* (the Matcher settled it
for at least one patient). Reporting only the first would call a categorical term
no patient record can answer "covered"; reporting only the second would divide by
a denominator that has already dropped everything the Parser gave up on. `score`
is checkable over every criterion the extraction produced, and `parse_score` /
`match_score` are the two halves it is made of, so a reader can tell a vocabulary
gap from a data gap.

**Derived from the checkpoint, never recounted.** Everything here reads
`parsed_criteria` and `matched_patients` — the same two fields the criteria table
and the per-criterion statuses are rendered from — and criteria are identified by
`attrition.criterion_key`, the identity the attrition panel and the what-if
simulator already use. A run's coverage therefore cannot disagree with the tables
beside it, which is the whole of AC 5. `structured_count` is public for the same
reason: the runs index's `criteria_count` column is this module's `structured`,
not a second count of the same lists.

**Before matching, coverage is provisional and says so.** A run parked at the gate
has no cohort, so nothing can be called resolved yet. `scored` is False there,
`match_score` is None, and `checkable` counts every structured criterion — the
reviewer at the gate is being told what the *parse* could cover, which is the only
honest answer available while the decision is still theirs.

**An unrecognized verdict is not a pass.** A status this build does not know, or a
criterion the cohort carries no result for at all, counts as unresolved: both are
exactly "we could not check this", and reading either as settled would inflate the
one figure this module exists to keep honest.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any, NamedTuple, TypedDict

from app.services.attrition import criterion_key
from app.services.checkpoint import mapping, rows
from app.services.criteria_edits import BUCKET_KINDS, UNPARSEABLE_BUCKET, criterion_label

# The criteria buckets a parse produces, and which side of the eligibility list
# each is — `BUCKET_KINDS` is `services/criteria_edits.py`'s own mapping, so the
# key this module builds for a criterion is the key the attrition panel and the
# what-if simulator address the same criterion by.
CRITERIA_BUCKETS = tuple(BUCKET_KINDS)

# The statuses that count as the Matcher having *settled* a criterion. Anything
# else — `unknown`, or a verdict from a future build — is a criterion this run
# could not check; see the module docstring.
_SETTLED = frozenset({"pass", "fail"})

# How many `unparseable` phrasings the cross-run aggregate ranks. The backlog
# question is "which wording should the vocabulary swallow next", and that is
# answered by the head of the ranking; a list of every phrasing an instance has
# ever seen is a different artifact. The aggregate also reports how many distinct
# phrasings there were, so a truncated list never reads as the whole of it.
PHRASE_DEPTH = 10

# Why a criterion could not be checked. `unparseable` is the Parser declining to
# invent structure for a sentence, `unresolved` the Matcher unable to settle a
# structured criterion — one is a vocabulary gap, the other a data gap, and they
# are fixed by different work.
#
# `gaps` reports them in that order: the sentences nobody screened on first (they
# are the reviewer's check-by-hand list and the vocabulary backlog), then the
# criteria the Matcher could not settle, ranked by how many patients they cost.
UNPARSEABLE = "unparseable"
UNRESOLVED = "unresolved"


class CoverageGap(TypedDict):
    """One thing this run could not check, and why.

    `text` is the verbatim protocol sentence for an `unparseable` gap and the
    criterion's rendered label for an `unresolved` one — the same label the
    criteria table, the report and the edit history name it with, so a reviewer
    reading this list can find the row it refers to.

    `kind` is "inclusion"/"exclusion" for a criterion and empty for a sentence
    that never became one. `patients` is how many patients the Matcher returned
    `unknown` for, and 0 on an `unparseable` gap — a sentence that never reached
    the Matcher cost every patient equally, and printing the cohort size there
    would imply a per-patient verdict that does not exist.
    """

    reason: str
    text: str
    kind: str
    patients: int


class CoverageSummary(TypedDict):
    """The score in the three figures a row or a cell needs.

    Shared verbatim with the runs index, which serves these three from the
    denormalized store columns rather than loading a checkpoint per row — so the
    cell in the index and the panel on the run detail view are one derivation
    read twice. `score` always comes from `score_of`, never from arithmetic at
    the point of rendering.
    """

    checkable: int
    criteria: int
    score: float


class Coverage(CoverageSummary):
    """One run's screenability, and what it is made of.

    `criteria` is every criterion the extraction produced — `structured` plus
    `unparseable` — so it is the denominator a reviewer counts in the criteria
    table. `resolved` and `unresolved` partition `structured` once a cohort
    exists; before that they are both 0 and `scored` is False.

    `checkable` is `structured` minus `unresolved`: the criteria this run both
    structured *and* settled. `parse_score` is `structured` over `criteria` and
    `match_score` `resolved` over `structured`, both rounded by the API so the
    figure a reviewer reads and the bar beside it are one value rather than two
    roundings of it. `match_score` is None rather than 0 before matching — a run
    with no cohort has not failed to resolve anything.
    """

    structured: int
    unparseable: int
    resolved: int
    unresolved: int
    scored: bool
    parse_score: float
    match_score: float | None
    gaps: list[CoverageGap]


class CoveragePhrase(TypedDict):
    """One `unparseable` phrasing, counted across runs.

    `count` is how many times it appeared across the window and `runs` how many
    runs' extractions contained it (a protocol can quote one requirement twice).
    `share` is `count` as a percentage of every `unparseable` sentence in the
    window, which is what makes the ranking readable as "this wording is a fifth of
    everything we cannot parse" — and the ranking is *by* `count`, so a bar drawn
    from `share` never runs against the order the rows are in.

    Grouped on the sentence with its whitespace collapsed and its case dropped —
    two runs of the same protocol must not rank as two different phrasings — and
    displayed in the wording of the most recent run that used it.
    """

    text: str
    runs: int
    count: int
    share: float


class CoverageAggregate(TypedDict):
    """Coverage across runs, plus the phrasings that cost the most of it (#93).

    `sampled` is how many runs were inspected and `total` how many exist, so a
    reader is never left to assume a window is the whole history. `runs` is the
    sampled runs that had an extraction at all — the population every figure
    below describes — and `scored` how many of those reached the Matcher.

    `score` is pooled (`checkable` over `criteria` across the window), not a mean
    of per-run scores: a two-criterion protocol should not swing the instance's
    figure as far as a forty-criterion one.

    `phrasings` is how many distinct `unparseable` wordings were seen, against
    `phrases`' capped ranking — a truncated list that did not say so would read
    as the whole backlog.
    """

    sampled: int
    total: int
    runs: int
    scored: int
    criteria: int
    checkable: int
    structured: int
    unparseable: int
    unresolved: int
    score: float
    phrasings: int
    phrases: list[CoveragePhrase]


def score_of(checkable: int, criteria: int) -> float:
    """`checkable` as a percentage of `criteria`, to one decimal.

    0.0 when there are no criteria — a share of nothing is not 100%, and a run
    with no extraction has no coverage to report rather than perfect coverage.
    The one definition of the score: the runs index recombines its two stored
    columns through this function rather than dividing them itself.
    """
    return round(checkable * 100 / criteria, 1) if criteria else 0.0


def structured_count(values: Mapping[str, Any]) -> int:
    """How many criteria the Parser turned into structured form, across the four
    buckets.

    `unparseable` is excluded, which is what makes it the *numerator* of the parse
    layer rather than the denominator: counting it would inflate "criteria found"
    with the exact sentences the Parser could not turn into criteria. Public
    because the runs index's `criteria_count` column is this number — one
    definition, so the column and the coverage panel cannot disagree.
    """
    criteria = mapping(values.get("parsed_criteria"))
    return sum(len(rows(criteria, bucket)) for bucket in CRITERIA_BUCKETS)


def _sentences(criteria: Mapping[str, Any]) -> list[str]:
    """The `unparseable` sentences, in the order the extraction carries them.

    Blank entries are dropped: an empty string is not a criterion nobody
    screened on, and counting one would both inflate the denominator and put an
    unreadable row at the top of the backlog. Only a hand-edited checkpoint has
    them — the Parser never writes one.
    """
    value = criteria.get(UNPARSEABLE_BUCKET)
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return []
    return [text for text in (str(item).strip() for item in value) if text]


class _Verdicts(NamedTuple):
    """How one criterion fared across the cohort, in patients.

    `settled` is patients the Matcher returned a pass or a fail for and
    `indeterminate` patients every result for this criterion came back `unknown`
    for. Patients, not rows: a criterion the protocol quotes twice must not count
    the same patient twice in the figure a reviewer reads.
    """

    settled: int
    indeterminate: int


def _verdicts(evaluations: Sequence[Mapping[str, Any]]) -> dict[str, _Verdicts]:
    """Per criterion, how many patients the Matcher settled it for.

    One entry per criterion the cohort actually carries a result for, keyed the
    way the attrition rows are (`attrition.criterion_key`) so a gap reported here
    names the same criterion that panel ranks.
    """
    tally: dict[str, list[int]] = {}
    for evaluation in evaluations:
        # Collapsed per patient first: within one patient a criterion is settled
        # if *any* of its results is a pass or a fail, so a duplicated criterion
        # cannot report one patient as both settled and indeterminate.
        settled: dict[str, bool] = {}
        for result in rows(evaluation, "criterion_results"):
            criterion = mapping(result.get("criterion"))
            if not criterion:
                # A result row carrying no criterion — a null in a hand-edited
                # checkpoint. There is nothing to attribute it to, and inventing a
                # key for it would put a phantom gap in the list.
                continue
            key = criterion_key(str(result.get("kind") or ""), criterion)
            settled[key] = settled.get(key, False) or str(result.get("status")) in _SETTLED
        for key, resolved in settled.items():
            counts = tally.setdefault(key, [0, 0])
            counts[0 if resolved else 1] += 1
    return {key: _Verdicts(counts[0], counts[1]) for key, counts in tally.items()}


def _gap(reason: str, text: str, kind: str, patients: int) -> CoverageGap:
    return CoverageGap(reason=reason, text=text, kind=kind, patients=patients)


def build_coverage(values: Mapping[str, Any]) -> Coverage:
    """Reduce a run's checkpoint to what it could and could not check.

    `values` is the checkpoint block of `GET /api/screenings/{id}/state` — the
    same argument `attrition.build_attrition` and `timeline.build_timeline` take,
    so a caller holding one payload derives all three without unpacking any of
    them. Only `parsed_criteria` and `matched_patients` are read.

    A run with no extraction yet gets an all-zero payload with no gaps: the shape
    is complete so no caller has to special-case a null, and `criteria == 0` is
    what tells the views to render nothing at all rather than "0% covered".
    """
    criteria = mapping(values.get("parsed_criteria"))
    evaluations = rows(values, "matched_patients")
    # The Matcher having run is what makes the second layer real. A run parked at
    # the gate is not a run whose criteria all failed to resolve.
    scored = bool(evaluations)
    verdicts = _verdicts(evaluations) if scored else {}

    structured = 0
    resolved = 0
    sentence_gaps = [_gap(UNPARSEABLE, sentence, "", 0) for sentence in _sentences(criteria)]
    unresolved_gaps: list[CoverageGap] = []
    for bucket, kind in BUCKET_KINDS.items():
        for criterion in rows(criteria, bucket):
            structured += 1
            if not scored:
                continue
            seen = verdicts.get(criterion_key(kind, criterion))
            if seen and seen.settled:
                resolved += 1
            else:
                # Either the Matcher returned `unknown` for every patient, or the
                # cohort carries no result for this criterion at all — a criterion
                # that was never applied is as uncheckable as one that could not be
                # settled, and calling it resolved is the inflation this avoids.
                unresolved_gaps.append(
                    _gap(
                        UNRESOLVED,
                        criterion_label(criterion),
                        kind,
                        seen.indeterminate if seen else len(evaluations),
                    )
                )

    # Most costly first, then by label so two exports of one run list them in the
    # same order rather than in dict-iteration order.
    unresolved_gaps.sort(key=lambda gap: (-gap["patients"], gap["text"]))

    unparseable = len(sentence_gaps)
    unresolved = len(unresolved_gaps)
    total = structured + unparseable
    checkable = structured - unresolved
    return Coverage(
        checkable=checkable,
        criteria=total,
        score=score_of(checkable, total),
        structured=structured,
        unparseable=unparseable,
        resolved=resolved,
        unresolved=unresolved,
        scored=scored,
        parse_score=score_of(structured, total),
        match_score=score_of(resolved, structured) if scored else None,
        gaps=[*sentence_gaps, *unresolved_gaps],
    )


def _phrase_key(text: str) -> str:
    """A sentence's identity across runs: whitespace collapsed, case dropped.

    Two uploads of one protocol — the case this ranking exists to accumulate over
    — differ in line wrapping far more often than in wording, and two spellings
    of one phrasing ranked apart would both fall below the cap that should have
    carried them.
    """
    return " ".join(text.split()).lower()


def aggregate(coverages: Iterable[Coverage], *, total: int | None = None) -> CoverageAggregate:
    """Pool per-run coverage into the instance's figure, and rank the phrasings.

    `coverages` is every run inspected, in the order they were read (newest
    first, as the runs index lists them) — including runs with no extraction,
    which count towards `sampled` and towards nothing else. `total` is how many
    runs exist, so the payload can state its own window; it defaults to the
    number sampled, which is the truth when the caller read everything.

    Pure, and given one list it returns one answer: the ranking's ties break on
    the normalized phrase, so the same window always ranks the same way.
    """
    inspected = list(coverages)
    parsed = [run for run in inspected if run["criteria"]]
    criteria = sum(run["criteria"] for run in parsed)
    checkable = sum(run["checkable"] for run in parsed)
    unparseable = sum(run["unparseable"] for run in parsed)

    # runs-per-phrase and occurrences-per-phrase, plus the wording to show. The
    # first wording seen wins, which — given the newest-first order above — is the
    # most recent run's, and a phrasing an older upload wrote differently does not
    # rewrite the label on the row.
    counts: dict[str, list[int]] = {}
    wording: dict[str, str] = {}
    for run in parsed:
        seen: set[str] = set()
        for gap in run["gaps"]:
            if gap["reason"] != UNPARSEABLE:
                continue
            key = _phrase_key(gap["text"])
            wording.setdefault(key, gap["text"])
            entry = counts.setdefault(key, [0, 0])
            entry[0] += 1
            if key not in seen:
                seen.add(key)
                entry[1] += 1

    # Most occurrences first, then the wording that spans the most runs, then the
    # normalized phrase so ties are reproducible. Ranked on `count` — the figure
    # `share` is computed from — because a caller drawing a bar per row from `share`
    # would otherwise render a ranking that gets longer as it goes down.
    ranked = sorted(counts.items(), key=lambda item: (-item[1][0], -item[1][1], item[0]))
    return CoverageAggregate(
        sampled=len(inspected),
        total=len(inspected) if total is None else total,
        runs=len(parsed),
        scored=sum(1 for run in parsed if run["scored"]),
        criteria=criteria,
        checkable=checkable,
        structured=sum(run["structured"] for run in parsed),
        unparseable=unparseable,
        unresolved=sum(run["unresolved"] for run in parsed),
        score=score_of(checkable, criteria),
        phrasings=len(counts),
        phrases=[
            CoveragePhrase(
                text=wording[key],
                runs=occurrences_runs,
                count=occurrences,
                share=score_of(occurrences, unparseable),
            )
            for key, (occurrences, occurrences_runs) in ranked[:PHRASE_DEPTH]
        ],
    )
