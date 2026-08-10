"""What-if threshold simulation (#95) — move a bound, watch the cohort move.

The attrition panel (#94) answers *what is killing the cohort*: "eGFR ≥ 60
excludes 41 of 100, and relaxing it would make 14 eligible". The question it
provokes immediately is the one this module answers — *and if it were 50?* Until
now the only way to find out was to edit the criteria, re-run the Critic, approve
again, and re-score every patient: minutes of work and a rewritten run, to test a
number a coordinator wanted to try three of.

**Why this is nearly free.** Quantitative checks are pure Python — a value and an
operator (`matcher.compare_quantitative`) — and since #95 the Matcher records the
value it compared against on every quantitative verdict (`observed`). So moving a
threshold is a re-application of that one comparison to numbers already in the
checkpoint. The categorical half is not re-evaluated at all: its verdicts stand
exactly as the run resolved them, term mapping and all. **A simulation therefore
makes no LLM call**, reads no patient record, and writes nothing — asserted by
tests, because "it's cheap" is the entire premise of offering it.

Four decisions worth knowing before editing this module:

**Only numeric thresholds can be simulated.** Relaxing a categorical criterion
("prior platinum chemotherapy" → "prior chemotherapy") would mean re-mapping the
new term against every patient's records, which is an LLM pass over the cohort —
the thing this feature exists *not* to do. An override naming a categorical
criterion is refused with that reason rather than silently ignored.

**Nothing is recomputed twice.** The simulated cohort is fed straight back through
`services/cohort.py` and `services/attrition.py`, so the simulated buckets are
produced by the same two rules as the real ones. A what-if panel whose "eligible"
column was computed differently from the cohort table beside it would be worse
than no panel — the reviewer would have no way to tell a real delta from a
disagreement between two implementations.

**A patient the run scored before #95 cannot be re-checked.** Their verdicts carry
no `observed` value, so their recorded status stands and the override reports how
many patients that happened to (`unavailable`). Silently holding those patients
at their old status while reporting a delta would understate the change; naming
them is what lets a reader discount the figure.

**The Critic check is the Critic's, not a second opinion.** A simulated threshold
is run through the deterministic `range` rules from the same rules file
(`graph/nodes/critic.run_deterministic_checks`), so a value this module flags is
exactly a value the Critic would block on promotion — and one it clears here will
clear there.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from math import isfinite
from typing import Any, TypedDict

from app.exceptions import InvalidSimulationError, ScreeningNotSimulatableError
from app.graph.nodes import critic, matcher
from app.services import attrition, cohort
from app.services.checkpoint import mapping, number, rows
from app.services.criteria_edits import BUCKET_KINDS as _BUCKET_KINDS
from app.services.criteria_edits import quantitative_label

# Which bucket of the extraction holds the numeric criteria an override can move.
# The bucket→kind mapping beside it is `criteria_edits.BUCKET_KINDS`, imported
# rather than restated: the key an override addresses a criterion by is built from
# it, and so is the key the attrition row it was dragged from carries.
QUANTITATIVE_BUCKETS = ("inclusion_quantitative", "exclusion_quantitative")


class Override(TypedDict):
    """One criterion's threshold, as the reviewer would have it.

    `key` is the criterion's `attrition.criterion_key` — the identity the panel
    rendered the row under, so an override can only ever address a criterion the
    reviewer was actually looking at. Only the comparison moves: the attribute and
    the unit are what the criterion *is about*, and changing either is an edit to
    the extraction, not a what-if about its bound.
    """

    key: str
    operator: str
    value: float
    value_high: float | None


class SimulatedOverride(TypedDict):
    """One override, echoed back with everything needed to render it inline.

    `before`/`after` are the criterion in the same one-line form the attrition rows
    and the edit history use, so a reviewer reads one wording of a criterion
    everywhere. `findings` are the deterministic Critic's verdict on the *new*
    threshold, and `unavailable` the patients this run scored without recording
    the value it compared, who therefore could not be re-checked.

    `key` addresses the criterion in `current`, `simulated_key` the same criterion
    in `simulated` — they differ because a criterion's identity *is* its rendered
    label (see `attrition.criterion_key`), and moving its threshold renames it.
    Stated rather than left to the caller to reconstruct: a panel pairing the two
    sides by hand would be re-deriving a rule that lives in another module.
    """

    key: str
    simulated_key: str
    kind: str
    attribute: str
    unit: str
    before: str
    after: str
    findings: list[dict[str, Any]]
    unavailable: int


class Simulation(TypedDict):
    """A what-if, both sides of it.

    `current` is the run as it stands and `simulated` the same reduction over the
    re-scored cohort — the full `CohortAttrition` on each side rather than bare
    counts, so the panel can show which criterion took over as the binding one
    once the dragged criterion stopped binding. `delta` is `simulated - current`
    per bucket, and can be negative.

    `criteria` is the whole extraction with the overrides applied: the payload a
    reviewer promotes verbatim through `PATCH /api/screenings/{id}/criteria`, so a
    promotion cannot drift from the simulation that motivated it. `criteria_revision`
    is what that PATCH sends as `base_revision`.
    """

    overrides: list[SimulatedOverride]
    current: attrition.CohortAttrition
    simulated: attrition.CohortAttrition
    delta: dict[str, int]
    criteria: dict[str, Any]
    criteria_revision: int


def _moved(criterion: Mapping[str, Any], override: Override) -> dict[str, Any]:
    """The criterion as the override would have it.

    `value_high` is cleared for any operator but `between` — leaving a stale upper
    bound behind would put a number in the promoted payload that the Matcher
    ignores and the criteria diff still reports as a change.
    """
    return {
        **criterion,
        "operator": override["operator"],
        "value": override["value"],
        "value_high": override["value_high"] if override["operator"] == "between" else None,
    }


def _index(criteria: Mapping[str, Any]) -> dict[str, tuple[str, Mapping[str, Any]]]:
    """Every criterion in the extraction as `key -> (bucket, criterion)`.

    All four buckets, not only the numeric two: an override naming a categorical
    criterion has to be told *why* it was refused, and that needs the difference
    between "no such criterion" and "not a numeric one".

    First writer wins, matching the attrition row a duplicate criterion merges
    into: two criteria that render identically are the same requirement quoted
    twice, and one override moves both (see `_promoted`).
    """
    index: dict[str, tuple[str, Mapping[str, Any]]] = {}
    for bucket, kind in _BUCKET_KINDS.items():
        for criterion in rows(criteria, bucket):
            index.setdefault(attrition.criterion_key(kind, criterion), (bucket, criterion))
    return index


def _resolve(
    index: Mapping[str, tuple[str, Mapping[str, Any]]], overrides: Sequence[Override]
) -> dict[str, Override]:
    """Overrides keyed by criterion, once each, every one of them addressable.

    Every rejection here is a 422 rather than an ignored entry: a panel that
    silently dropped an override would report the *unchanged* cohort as the
    simulated one, which is a wrong answer dressed as a right one.
    """
    resolved: dict[str, Override] = {}
    for override in overrides:
        key = override["key"]
        if key in resolved:
            raise InvalidSimulationError(
                f"Criterion “{key}” is overridden twice in one simulation; give it one threshold."
            )
        entry = index.get(key)
        if entry is None:
            raise InvalidSimulationError(
                f"This run has no criterion “{key}”. Simulate against the criteria it was "
                "actually scored with — reload the run if it has been edited since."
            )
        bucket, criterion = entry
        if bucket not in QUANTITATIVE_BUCKETS:
            raise InvalidSimulationError(
                f"“{key}” is a categorical criterion, and only numeric thresholds can be "
                "simulated. Changing a term would mean re-matching it against every patient's "
                "records, which is an LLM pass over the whole cohort rather than a what-if."
            )
        if "attribute" not in criterion:
            # In a numeric bucket but carrying nothing to compare against — a
            # hand-edited checkpoint, not an extraction the Parser produced.
            # Refused here because everything downstream assumes the attribute is
            # there: the Critic's range check reads it unconditionally, and a
            # KeyError on a read-only endpoint is a 500 handed to a reader for a
            # row they did not write.
            raise InvalidSimulationError(
                f"“{key}” names no patient attribute to compare against, so there is no "
                "threshold to move. Correct the criterion at the review gate instead."
            )
        high = override["value_high"]
        if override["operator"] == "between":
            if high is None:
                raise InvalidSimulationError(
                    f"A 'between' threshold for “{key}” needs an upper bound as well as a "
                    "lower one."
                )
            if high < override["value"]:
                # An empty interval: `value <= v <= value_high` holds for nobody, so
                # this would answer "no patient is eligible" and carry the inverted
                # bound straight into the promotable payload. The Critic's range
                # check only inspects the lower bound, so nothing downstream would
                # catch it either.
                raise InvalidSimulationError(
                    f"The 'between' threshold for “{key}” has its bounds the wrong way round "
                    f"({override['value']} to {high}), which no patient could satisfy."
                )
        # Rejected here rather than by the request model, which would be the
        # obvious place: FastAPI echoes the offending input back inside its 422
        # body, and a NaN echoed into a JSON response is what starlette refuses to
        # serialize — turning a malformed threshold into a 500. Caught in the
        # domain, it is a 422 in the same error contract as every other rejection.
        # (Python's JSON parser accepts the non-standard `NaN`/`Infinity` literals,
        # and a NaN bound compares false against every value — so it would answer
        # "nobody is eligible" with a straight face.)
        bounds = [
            override["value"],
            *([] if override["value_high"] is None else [override["value_high"]]),
        ]
        if not all(isfinite(bound) for bound in bounds):
            raise InvalidSimulationError(f"The threshold for “{key}” has to be a real number.")
        resolved[key] = override
    return resolved


def _rescore(
    evaluations: Sequence[Mapping[str, Any]], resolved: Mapping[str, Override]
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Re-apply the overridden criteria to the cohort the run already scored.

    Returns the simulated evaluations and, per override, how many *patients* could
    not be re-checked — patients, not rows, so a criterion the protocol quotes
    twice does not report double the cohort's worth of them (the same rule the
    attrition counts follow).

    The simulated evaluations carry only what a bucket count and an attrition
    reduction read — the statuses, the observed values and the eligibility flags —
    and deliberately no `summary` or per-criterion `explanation`: those are
    verdicts about a patient, and a simulation produces none. Nobody approved it,
    and no patient was matched against it.
    """
    simulated: list[dict[str, Any]] = []
    unavailable = dict.fromkeys(resolved, 0)

    for evaluation in evaluations:
        results: list[dict[str, Any]] = []
        unchecked: set[str] = set()
        for result in rows(evaluation, "criterion_results"):
            criterion = mapping(result.get("criterion"))
            kind = str(result.get("kind") or "")
            override = resolved.get(attrition.criterion_key(kind, criterion)) if criterion else None
            # `observed` is carried through so the simulated breakdown reports the
            # same value span the current one does — a moved row whose slider
            # bounds came back empty would be a trap for the next caller.
            row = {
                "criterion": criterion,
                "kind": kind,
                "status": str(result.get("status") or ""),
                **({} if "observed" not in result else {"observed": result["observed"]}),
            }
            # Not overridden, or a categorical criterion that merely renders like
            # one that is: its verdict stands exactly as the run resolved it.
            if override is None or "attribute" not in criterion:
                results.append(row)
                continue
            # Narrowed rather than trusted: an unnarrowed value reaching the `>=`
            # inside `compare_quantitative` raises TypeError on a string, and a
            # read-only endpoint must not 500 over a malformed checkpoint row.
            observed = number(result.get("observed")) if "observed" in result else None
            if "observed" not in result:
                # Scored before the Matcher recorded the value it compared. The
                # recorded status is the only thing still true about this patient
                # and this criterion, so it stands — and is counted, so the delta
                # is read for what it is.
                unchecked.add(override["key"])
                results.append(row)
                continue
            moved = _moved(criterion, override)
            status = matcher.compare_quantitative(observed, moved)
            results.append(
                {
                    **row,
                    "criterion": moved,
                    # An exclusion the patient now meets rules them out, exactly as
                    # it does in the Matcher's own loop.
                    "status": matcher.flip_exclusion(status) if kind == "exclusion" else status,
                }
            )
        for key in unchecked:
            unavailable[key] += 1
        eligible, needs_review = matcher.verdict(results)
        simulated.append(
            {
                "patient_id": evaluation.get("patient_id"),
                "name": evaluation.get("name"),
                "eligible": eligible,
                "needs_review": needs_review,
                "criterion_results": results,
            }
        )
    return simulated, unavailable


def _promoted(criteria: Mapping[str, Any], resolved: Mapping[str, Override]) -> dict[str, Any]:
    """The whole extraction with the overrides applied, ready to be promoted.

    Only the two numeric buckets are rebuilt, and only their overridden entries
    change; everything else — including anything malformed — is passed through
    verbatim rather than normalized, so promoting a simulation submits the
    reviewer's own extraction and not this module's reading of it. The PATCH
    validates it against `CriteriaSchema` either way.
    """
    promoted = dict(criteria)
    for bucket in QUANTITATIVE_BUCKETS:
        kind = _BUCKET_KINDS[bucket]
        source = criteria.get(bucket)
        if not isinstance(source, Sequence) or isinstance(source, str | bytes):
            continue
        rebuilt: list[Any] = []
        for criterion in source:
            override = (
                resolved.get(attrition.criterion_key(kind, criterion))
                if isinstance(criterion, Mapping)
                else None
            )
            rebuilt.append(_moved(criterion, override) if override else criterion)
        promoted[bucket] = rebuilt
    return promoted


def _findings(criterion: Mapping[str, Any], bucket: str, rules: list[dict]) -> list[dict[str, Any]]:
    """The deterministic Critic's verdict on one simulated threshold.

    Run through `critic.run_deterministic_checks` on a one-criterion extraction
    rather than reimplemented, so a value flagged here is exactly a value the
    Critic would block when the override is promoted. `rules` is pre-filtered to
    the `range` checks by the caller: the other three read the protocol text or the
    extraction as a whole, and firing them against a single criterion in isolation
    would report a missing age bound for every simulation.

    Parity is why the upper bound of a `between` is not checked here — the Critic
    does not check it either, and a simulator that flagged what the Critic then
    cleared would be the more confusing of the two wrong answers.
    """
    extraction: dict[str, Any] = {
        "inclusion_quantitative": [],
        "inclusion_categorical": [],
        "exclusion_quantitative": [],
        "exclusion_categorical": [],
        "unparseable": [],
    }
    # The rule engine interpolates `unit` and `source_text` into its finding text
    # without checking for them; `attribute` is guaranteed by `_resolve`. Defaults
    # first so a real value always wins — a checkpoint missing a unit should read
    # as a finding with no unit, not as a 500.
    extraction[bucket] = [{"unit": "", "source_text": "", **criterion}]
    return critic.run_deterministic_checks(extraction, "", rules)


def simulate(values: Mapping[str, Any], overrides: Sequence[Override]) -> Simulation:
    """Re-score a run's cohort under moved thresholds, without touching it.

    `values` is the checkpoint block of `GET /api/screenings/{id}/state` — the same
    argument `attrition.build_attrition` and `timeline.build_timeline` take. Nothing
    here writes; the caller holds a read-only snapshot and gets a payload back.

    Refuses (409) a run with no scored cohort or no extraction: there is nothing to
    re-score, and answering with an all-zero table would read as "relaxing this
    changes nothing" rather than "this run never ran".
    """
    evaluations = rows(values, "matched_patients")
    criteria = values.get("parsed_criteria")
    if not evaluations or not isinstance(criteria, Mapping):
        raise ScreeningNotSimulatableError(
            "This screening has not scored a cohort yet, so there is nothing to simulate against. "
            "Approve it at the gate first."
        )

    index = _index(criteria)
    resolved = _resolve(index, overrides)
    simulated_cohort, unavailable = _rescore(evaluations, resolved)

    # Loaded once for the whole simulation rather than per override, and filtered
    # to the checks a single criterion in isolation can answer — see `_findings`.
    range_rules = [rule for rule in critic.load_rules() if rule["check"] == "range"]
    echoed: list[SimulatedOverride] = []
    for key, override in resolved.items():
        # The extraction is the authority on what the criterion *was*: it is the
        # same object the Matcher scored the cohort against.
        bucket, original = index[key]
        moved = _moved(original, override)
        echoed.append(
            SimulatedOverride(
                key=key,
                simulated_key=attrition.criterion_key(_BUCKET_KINDS[bucket], moved),
                kind=_BUCKET_KINDS[bucket],
                attribute=str(original.get("attribute") or ""),
                unit=str(original.get("unit") or ""),
                # `quantitative_label` on both sides, which is also what the
                # attrition row was labelled with — one wording of a criterion
                # everywhere a reviewer meets it.
                before=quantitative_label(original),
                after=quantitative_label(moved),
                findings=_findings(moved, bucket, range_rules),
                unavailable=unavailable[key],
            )
        )

    # Both sides through `services/cohort.py`, so the delta is a difference of two
    # counts of one rule rather than of two implementations of it.
    before = cohort.bucket_counts(evaluations)
    after = cohort.bucket_counts(simulated_cohort)
    return Simulation(
        overrides=echoed,
        current=attrition.build_attrition(values),
        simulated=attrition.build_attrition({"matched_patients": simulated_cohort}),
        delta={bucket: after[bucket] - before[bucket] for bucket in cohort.BUCKET_ORDER},
        criteria=_promoted(criteria, resolved),
        criteria_revision=int(values.get("criteria_revision") or 0),
    )
