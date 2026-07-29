"""Reviewer edits to a parsed extraction: the before/after diff (#53).

The human-in-the-loop gate lets a reviewer correct the extraction and re-run
instead of only approving it, so "what did the human change?" has to be an
answerable question long after the session that changed it. This module turns two
revisions of `parsed_criteria` into a list of human-readable
`CriteriaChange` records, which `services.screening` stamps into the checkpoint
alongside the editor's identity.

Criteria are matched across revisions by their `source_text` — the verbatim
protocol sentence — not by list position. Provenance is the only stable identity
a criterion has: a reviewer who deletes a hallucinated criterion shifts every
index after it, and an index-wise diff would report that as "everything below
changed". Matching on provenance also makes a *reclassification* legible: an
`unparseable` sentence turned into a real criterion keeps its text, so the two
halves pair up into one change instead of an unexplained removal plus an
unexplained addition.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from app.auth import Principal
from app.graph.state import CriteriaChange, CriteriaEdit

# The buckets a diff walks, in the order changes are reported. `unparseable`
# is included (unlike the criteria *count* in services.screening, which excludes
# it) precisely because reclassifying out of it is one of the edits this exists
# to record.
QUANTITATIVE_BUCKETS = ("inclusion_quantitative", "exclusion_quantitative")
CATEGORICAL_BUCKETS = ("inclusion_categorical", "exclusion_categorical")
UNPARSEABLE_BUCKET = "unparseable"
DIFFED_BUCKETS = (*QUANTITATIVE_BUCKETS, *CATEGORICAL_BUCKETS, UNPARSEABLE_BUCKET)


def _number(value: Any) -> str:
    """A threshold as a clinician writes it: `18`, not the schema's `18.0`.

    `value` is a float on the wire (so 1.5 x 10^9/L survives), which would
    otherwise render every whole-number bound with a spurious decimal — and a
    diff line reading "age >= 18.0 years → age >= 65.0 years" invites the reader
    to wonder what the .0 means.
    """
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return "" if value is None else str(value)


def _quantitative_label(criterion: Mapping[str, Any]) -> str:
    """e.g. `age >= 18 years`, `egfr between 30–60 mL/min/1.73m2`."""
    high = (
        f"–{_number(criterion.get('value_high'))}" if criterion.get("operator") == "between" else ""
    )
    parts = [
        str(criterion.get("attribute", "")),
        str(criterion.get("operator", "")),
        f"{_number(criterion.get('value'))}{high}",
        str(criterion.get("unit", "")),
    ]
    return " ".join(p for p in parts if p).strip()


def _categorical_label(criterion: Mapping[str, Any]) -> str:
    """e.g. `NSCLC (diagnosis)`, `NOT active infection (condition)`."""
    negated = "NOT " if criterion.get("negated") else ""
    return f"{negated}{criterion.get('value', '')} ({criterion.get('category', '')})"


def _entry(bucket: str, item: Any) -> tuple[str, str]:
    """One bucket item as `(provenance_key, label)`.

    The key is the normalized `source_text`, which is what pairs a criterion with
    its counterpart in the other revision. Falls back to the label when a
    criterion carries no provenance, so an entry never keys on the empty string
    and pairs with an unrelated one.
    """
    if bucket == UNPARSEABLE_BUCKET:
        text = str(item)
        return text.strip().lower(), text
    label = (
        _quantitative_label(item) if bucket in QUANTITATIVE_BUCKETS else _categorical_label(item)
    )
    provenance = str(item.get("source_text") or "").strip().lower()
    return provenance or f"label:{label.lower()}", label


def _entries(criteria: Mapping[str, Any] | None, bucket: str) -> list[tuple[str, str]]:
    items = (criteria or {}).get(bucket) or []
    if not isinstance(items, Sequence) or isinstance(items, str | bytes):
        return []
    return [_entry(bucket, item) for item in items]


def _change(
    bucket: str,
    kind: str,
    before: str | None,
    after: str | None,
    from_bucket: str | None = None,
) -> CriteriaChange:
    return CriteriaChange(
        bucket=bucket, from_bucket=from_bucket, kind=kind, before=before, after=after
    )


def diff_criteria(
    before: Mapping[str, Any] | None, after: Mapping[str, Any] | None
) -> list[CriteriaChange]:
    """Every difference between two revisions of `parsed_criteria`.

    Reported in three passes so the result reads in order of consequence:
    in-place modifications first (bucket by bucket), then removals and
    reclassifications, then additions. An empty list means the reviewer submitted
    the extraction unchanged.

    Duplicate provenance within a bucket pairs up positionally among the
    duplicates, so two criteria quoting the same sentence still diff 1:1 rather
    than both matching the first one.
    """
    modified: list[CriteriaChange] = []
    removed: list[tuple[str, str, str]] = []  # (bucket, key, label)
    added: list[tuple[str, str, str]] = []

    for bucket in DIFFED_BUCKETS:
        unmatched_after = _entries(after, bucket)
        for key, label in _entries(before, bucket):
            position = next((i for i, (k, _) in enumerate(unmatched_after) if k == key), None)
            if position is None:
                removed.append((bucket, key, label))
                continue
            _, after_label = unmatched_after.pop(position)
            if after_label != label:
                modified.append(_change(bucket, "modified", label, after_label))
        added += [(bucket, key, label) for key, label in unmatched_after]

    # Second pass: a removal whose provenance reappears in a *different* bucket is
    # a reclassification (the `unparseable` → real-criterion case), not a delete.
    tail: list[CriteriaChange] = []
    for bucket, key, label in removed:
        position = next((i for i, (_, k, _) in enumerate(added) if k == key), None)
        if position is None:
            tail.append(_change(bucket, "removed", label, None))
            continue
        to_bucket, _, after_label = added.pop(position)
        tail.append(_change(to_bucket, "reclassified", label, after_label, from_bucket=bucket))

    return [
        *modified,
        *tail,
        *(_change(bucket, "added", None, label) for bucket, _, label in added),
    ]


def edit_record(revision: int, editor: Principal, changes: list[CriteriaChange]) -> CriteriaEdit:
    """The audit entry for one revision — who changed what, and when."""
    return CriteriaEdit(
        revision=revision,
        edited_by=editor.email,
        edited_by_role=editor.role,
        edited_at=datetime.now(UTC).isoformat(),
        changes=changes,
    )


def summarize(changes: list[CriteriaChange]) -> str:
    """A one-line count of a revision's changes, for the run's event log.

    Deliberately counts rather than lists: the event log is a timeline, and the
    full before/after lives in `criteria_edits` for anyone who wants the detail.
    """
    if not changes:
        return "no changes"
    tally: dict[str, int] = {}
    for change in changes:
        tally[change["kind"]] = tally.get(change["kind"], 0) + 1
    return ", ".join(f"{count} {kind}" for kind, count in sorted(tally.items()))
