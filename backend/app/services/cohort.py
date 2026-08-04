"""Which bucket an evaluated patient lands in — one rule, every reader.

The Matcher writes `matched_patients` as a flat list of evaluations, each carrying
`eligible` and `needs_review`. Turning that pair into the triage bucket a human
reads ("Eligible" / "Needs review" / "Ineligible") is a one-line rule, and it was
written out four times: the runs index's match count
(`services.screening._match_count`), the exported report (`services.report`), the
comparison of two runs (`services.comparison`), and the cohort table in the
browser (`frontend PatientMatchTable.bucketOf`).

Three of those four now read it from here. Four renderings of one run must not
disagree about who was eligible — a report saying 12 eligible beside an index row
saying 11 is a defect a reader cannot resolve without the raw checkpoint.

`needs_review` outranks `eligible` everywhere: a patient the Matcher could not
fully determine has to reach a human even if every criterion it *could* evaluate
passed, so counting them as eligible would quietly hand a coordinator an
unreviewed candidate.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

# Worst-last, which is the order the cohort's counts are shown in.
BUCKET_ORDER = ("eligible", "review", "ineligible")

# Reviewer-facing wording for each bucket, rendered server-side so the report and
# the comparison view name a bucket identically.
BUCKET_LABELS = {"eligible": "Eligible", "review": "Needs review", "ineligible": "Ineligible"}


def bucket_of(evaluation: Mapping[str, Any]) -> str:
    """The triage bucket one patient evaluation belongs to."""
    if evaluation.get("needs_review"):
        return "review"
    return "eligible" if evaluation.get("eligible") else "ineligible"


def bucket_counts(cohort: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """How many patients fell in each bucket — every bucket present, including 0.

    Zeros are kept rather than omitted: "0 ineligible" is a fact about the run,
    and a caller rendering a fixed set of chips would otherwise have to invent the
    missing keys itself.
    """
    counts = dict.fromkeys(BUCKET_ORDER, 0)
    for evaluation in cohort:
        bucket = bucket_of(evaluation)
        # A bucket outside BUCKET_ORDER can only appear if bucket_of grows a new
        # one; count it rather than dropping the patient from the total.
        counts[bucket] = counts.get(bucket, 0) + 1
    return counts


def matched_count(cohort: Iterable[Mapping[str, Any]]) -> int:
    """How many patients the run actually matched — the eligible bucket alone.

    `matched_patients` is the whole evaluated cohort, so a run that scored 300
    patients and cleared none is a 0-match run, and the runs index should say so.
    """
    return sum(1 for evaluation in cohort if bucket_of(evaluation) == "eligible")
