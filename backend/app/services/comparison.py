"""Two runs, side by side: what the criteria and the cohort did differently (#59).

The same protocol screened twice does not have to produce the same run. The
Parser is an LLM, the Critic can push an extraction back, a reviewer can correct
it by hand (#53), and the rules file is deployment configuration that can be
amended — so "we re-ran it, did anything change?" is a question a coordinator
asks routinely and could previously only answer by opening two tabs and reading.
It is also how two *different* protocols get compared: a screening amendment
against the version it replaces.

This module is the reduction. It takes two `get_screening_state` payloads — the
same payload the run detail view and the exported report are rendered from — and
returns one comparison: a header block per run, the criteria paired up, and the
cohort's verdicts paired up.

Three decisions worth knowing before editing:

**Pure, and fed the same payload as everything else.** Nothing here reads the
store or the graph; `services.screening.compare_screenings` fetches both states
and hands them over, exactly as it does for the report. So a comparison cannot
disagree with either run's own detail page about what that run contains — there is
one read path, used three times.

**Criteria are paired by provenance, not by position.** A run with one criterion
deleted shifts every index after it, and an index-wise pairing would report that
as "everything below changed". `services.criteria_edits` already solved this for
the reviewer-edit diff, and this module reuses its keying and its labels
(`bucket_entries`) rather than growing a second, drifting notion of what makes two
criteria the same one. The one difference is what comes out: an edit diff lists
only changes, while a side-by-side has to carry the unchanged rows too, since the
whole point is reading one run's extraction against the other's.

**Pairing is within a bucket.** A criterion that moved from inclusion to
exclusion between two runs reads as a removal on one side and an addition on the
other, rather than as the `reclassified` entry an edit diff would produce. That is
the honest reading here: these are two independent extractions, and "the same
sentence became an exclusion criterion" is a difference a reviewer should see
stated on both sides of the table, in both buckets, not folded into one row.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from app.services import cohort, criteria_edits

# The buckets a comparison walks, in reading order — the four criteria buckets
# plus `unparseable`, which is exactly where a re-parse's differences tend to show
# up first (one run read a sentence, the other gave up on it).
COMPARED_BUCKETS = criteria_edits.DIFFED_BUCKETS

# `unparseable` holds sentences the Parser could not turn into criteria, so it is
# excluded from a run's criteria count for the same reason
# `services.screening._CRITERIA_BUCKETS` excludes it: counting it would inflate
# "criteria found" with the lines that were not found.
_COUNTED_BUCKETS = tuple(b for b in COMPARED_BUCKETS if b != criteria_edits.UNPARSEABLE_BUCKET)

# How the criteria rows are labelled. `unchanged` is not a difference and carries
# no highlight; the other three are what the issue asks to be highlighted.
_UNCHANGED = "unchanged"
_MODIFIED = "modified"
_ADDED = "added"
_REMOVED = "removed"

# Cohort row kinds, in the order rows are listed: a patient whose verdict moved is
# the finding, a patient only one run scored is context, and the patients both runs
# agreed on are the bulk and go last.
_MATCH_KINDS = ("changed", "only_a", "only_b", "same")


def _mapping(value: Any) -> Mapping[str, Any]:
    """`value` as a mapping, or an empty one — a run with no checkpoint has none."""
    return value if isinstance(value, Mapping) else {}


def _cohort(values: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The run's evaluated cohort, skipping anything not shaped like an evaluation."""
    patients = values.get("matched_patients") or []
    if not isinstance(patients, Sequence) or isinstance(patients, str | bytes):
        return []
    return [p for p in patients if isinstance(p, Mapping)]


def _phase(payload: Mapping[str, Any]) -> str:
    """The run's phase, derived exactly as the detail view and the report derive it.

    `pending` wins (the graph is parked at the gate), then the store row — a run
    uploaded but never streamed has an empty `values`, and reading `current_step`
    out of it would render that run as finished.
    """
    if payload.get("pending"):
        return "awaiting_approval"
    record = _mapping(payload.get("screening"))
    values = _mapping(payload.get("values"))
    return str(record.get("status") or values.get("current_step") or "")


def _criteria_total(criteria: Mapping[str, Any]) -> int:
    """Criteria across the four real buckets — the figure the runs index shows."""
    return sum(len(criteria_edits.bucket_entries(criteria, b)) for b in _COUNTED_BUCKETS)


def _run_block(payload: Mapping[str, Any], side: str) -> dict[str, Any]:
    """One run's header: which run it is, and the totals the columns are read under.

    The counts come from the checkpoint the rows below are built from, not from the
    store's denormalized columns: those are written when a run reaches a terminal
    frame, so for a run parked mid-pipeline they can lag the criteria this
    comparison is actually showing. A header disagreeing with the table under it is
    worse than a header that is one refresh behind the index.
    """
    values = _mapping(payload.get("values"))
    record = _mapping(payload.get("screening"))
    criteria = _mapping(values.get("parsed_criteria"))
    patients = _cohort(values)
    return {
        # "a"/"b" as the query parameters named them, so a client can tell which
        # column a row's `a`/`b` fields belong to without matching thread ids.
        "side": side,
        "thread_id": str(record.get("thread_id") or ""),
        "source_filename": str(
            record.get("source_filename") or values.get("source_filename") or ""
        ),
        "status": _phase(payload),
        "created_at": str(record.get("created_at") or ""),
        "trial_title": str(criteria.get("trial_title") or ""),
        # 0 for the Parser's own extraction, N for the Nth reviewer revision (#53) —
        # the first thing to check when two runs of one protocol disagree.
        "criteria_revision": int(values.get("criteria_revision") or 0),
        "criteria_count": _criteria_total(criteria),
        "cohort": {**cohort.bucket_counts(patients), "total": len(patients)},
        # Whether there is an extraction to compare at all: a run that was uploaded
        # but never streamed has no criteria, and its empty column has to read as
        # "never ran" rather than as "the Parser found nothing".
        "parsed": bool(criteria),
        "matched": bool(patients),
    }


def _pair_bucket(
    a_criteria: Mapping[str, Any], b_criteria: Mapping[str, Any], bucket: str
) -> list[dict[str, Any]]:
    """One bucket's criteria paired across the two runs, in A's order then B's extras.

    Two passes, because the two things this view compares have different notions of
    "the same criterion":

    1. **Provenance.** `criteria_edits.bucket_entries`' key is the verbatim protocol
       sentence, so a criterion both runs read out of the same sentence lands on one
       row even if it sits at a different index — and if its threshold moved, that is
       exactly the `modified` row a re-parse comparison exists to show. Two criteria
       quoting one sentence pair up positionally among themselves.
    2. **The criterion itself.** Whatever is left over is matched on its rendered
       label. Without this, two *different* protocols — the other half of what this
       view is for — would report `age >= 18 years` as removed from one and added to
       the other purely because each quoted its own eligibility section. The
       criterion is identical; only the sentence behind it differs, and that is not a
       difference in what the trials require. The same holds for a re-parse where the
       model quoted a longer sentence for an unchanged criterion.

    A's rows come first, in A's own order, with B-only criteria appended: a
    coordinator reads the run they know down the left, and the additions the other
    run made are the tail. Sorting would scramble the extraction's own
    inclusion-before-exclusion sense within the bucket.
    """
    unmatched_b = criteria_edits.bucket_entries(b_criteria, bucket)
    rows: list[dict[str, Any]] = []
    for key, label in criteria_edits.bucket_entries(a_criteria, bucket):
        position = next((i for i, (k, _) in enumerate(unmatched_b) if k == key), None)
        if position is None:
            # Provisional: the label pass below may still find this one a partner.
            rows.append({"kind": _REMOVED, "a": label, "b": None})
            continue
        _, b_label = unmatched_b.pop(position)
        kind = _UNCHANGED if b_label == label else _MODIFIED
        rows.append({"kind": kind, "a": label, "b": b_label})

    for row in rows:
        if row["kind"] != _REMOVED:
            continue
        position = next((i for i, (_, label) in enumerate(unmatched_b) if label == row["a"]), None)
        if position is None:
            continue
        unmatched_b.pop(position)
        row["kind"] = _UNCHANGED
        row["b"] = row["a"]

    rows += [{"kind": _ADDED, "a": None, "b": label} for _, label in unmatched_b]
    return rows


def _compare_criteria(a_values: Mapping[str, Any], b_values: Mapping[str, Any]) -> dict[str, Any]:
    """Both extractions, bucket by bucket, with every difference typed.

    `added`/`removed` are stated from A's point of view — B has a criterion A does
    not, A has one B does not — which is the only reading that makes sense of a
    two-column table whose left column is A.

    Buckets neither run used are omitted entirely: an empty `unparseable` section
    on both sides is noise, and the totals already say whether anything differs.
    """
    a_criteria = _mapping(a_values.get("parsed_criteria"))
    b_criteria = _mapping(b_values.get("parsed_criteria"))
    totals = dict.fromkeys((_UNCHANGED, _MODIFIED, _ADDED, _REMOVED), 0)
    buckets: list[dict[str, Any]] = []
    for bucket in COMPARED_BUCKETS:
        rows = _pair_bucket(a_criteria, b_criteria, bucket)
        if not rows:
            continue
        for row in rows:
            totals[row["kind"]] += 1
        buckets.append({"bucket": bucket, "rows": rows})
    differences = totals[_MODIFIED] + totals[_ADDED] + totals[_REMOVED]
    return {
        "buckets": buckets,
        "totals": totals,
        "differences": differences,
        # True only when there was something to compare: two runs that both never
        # parsed are not "identical extractions", they are two absent ones.
        "identical": differences == 0 and bool(a_criteria) and bool(b_criteria),
    }


def _by_patient(values: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """One run's cohort indexed by patient id, first evaluation per id winning."""
    indexed: dict[str, Mapping[str, Any]] = {}
    for evaluation in _cohort(values):
        indexed.setdefault(str(evaluation.get("patient_id") or ""), evaluation)
    return indexed


def _verdict(evaluation: Mapping[str, Any] | None) -> dict[str, str] | None:
    """One patient's verdict in one run, or None when that run never scored them."""
    if evaluation is None:
        return None
    bucket = cohort.bucket_of(evaluation)
    return {"bucket": bucket, "label": cohort.BUCKET_LABELS.get(bucket, bucket)}


def _compare_matches(a_values: Mapping[str, Any], b_values: Mapping[str, Any]) -> dict[str, Any]:
    """The two cohorts paired by patient, verdict against verdict.

    Paired on `patient_id`, which is the EHR's own key — the one identity that
    survives two independent runs. `changed` is the row a reviewer is looking for:
    the same patient, a different verdict, because the criteria moved underneath
    them. `only_a`/`only_b` cover a patient one run scored and the other did not,
    which is what two runs against different cohort snapshots look like.

    Every patient is listed, not only the differences: this is the cohort's
    reconciliation, and "the other 280 agreed" is only credible if the rows are
    there. Each row carries a verdict and a name, never the per-criterion results —
    those are on each run's own page, and duplicating them here would put two full
    evaluations per patient on the wire for a view that shows neither.

    A patient id repeated *within* one run's cohort is an EHR defect rather than
    two patients, so the first evaluation for an id is the one compared and the
    duplicates are ignored — one row per person, which is what the pairing means.
    """
    a_cohort = _by_patient(a_values)
    b_cohort = _by_patient(b_values)
    rows: list[dict[str, Any]] = []
    for patient_id in sorted(a_cohort.keys() | b_cohort.keys()):
        a_patient = a_cohort.get(patient_id)
        b_patient = b_cohort.get(patient_id)
        a_verdict = _verdict(a_patient)
        b_verdict = _verdict(b_patient)
        if a_verdict is None:
            kind = "only_b"
        elif b_verdict is None:
            kind = "only_a"
        else:
            kind = "same" if a_verdict["bucket"] == b_verdict["bucket"] else "changed"
        rows.append(
            {
                "patient_id": patient_id,
                # Either run's name for them; they are the same person, and a run
                # that only B scored still has to render a name.
                "name": str((a_patient or b_patient or {}).get("name") or ""),
                "kind": kind,
                "a": a_verdict,
                "b": b_verdict,
            }
        )
    totals = dict.fromkeys(_MATCH_KINDS, 0)
    for row in rows:
        totals[row["kind"]] += 1
    # Differences first, agreement last (see _MATCH_KINDS), each block in patient-id
    # order — a stable sort over the id-ordered list above, so the order is
    # deterministic across two requests for the same pair.
    rows.sort(key=lambda row: _MATCH_KINDS.index(row["kind"]))
    return {
        "patients": rows,
        "totals": totals,
        "differences": totals["changed"] + totals["only_a"] + totals["only_b"],
        # Whether both runs got as far as scoring anyone. One that stopped at the
        # approval gate has no cohort, and its empty column means "not yet", not
        # "nobody was eligible".
        "compared": bool(a_cohort) and bool(b_cohort),
    }


def compare_runs(a_payload: Mapping[str, Any], b_payload: Mapping[str, Any]) -> dict:
    """Two `get_screening_state` payloads as one side-by-side comparison (#59).

    `runs` carries the two header blocks in `[a, b]` order — the order the request
    named them — and every row below states its A side and its B side under those
    same keys, so a client never has to guess which column it is rendering.
    """
    a_values = _mapping(a_payload.get("values"))
    b_values = _mapping(b_payload.get("values"))
    return {
        "runs": [_run_block(a_payload, "a"), _run_block(b_payload, "b")],
        "criteria": _compare_criteria(a_values, b_values),
        "matches": _compare_matches(a_values, b_values),
    }
