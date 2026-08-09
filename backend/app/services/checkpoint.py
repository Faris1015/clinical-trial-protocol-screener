"""Reading a checkpoint defensively — the guards a derived view needs.

A module under `services/` that reduces a run's state into a view reads a
LangGraph checkpoint that may have been written by an older build of the
pipeline, by a run that failed partway, or (in a demo deployment) by hand. A
wrongly typed entry has to degrade to an empty section rather than raise on a page
load, and the attrition breakdown (#94) and the what-if simulator (#95) have to do
it *identically* — one derives the other's input, so two readers disagreeing about
whether a malformed row counts would put a delta between a run and itself.

Those two are the current callers. `report.py`, `timeline.py` and `comparison.py`
predate this module and still carry their own copies; folding them in is a
worthwhile tidy-up, not a correctness fix, since none of them feeds another.

Deliberately not validation. Nothing here reports a problem or repairs one; it
narrows an `Any` from the checkpoint to the shape the caller can walk, and leaves
the caller to decide what an absent row means.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def mapping(item: Any) -> Mapping[str, Any]:
    """One entry of the checkpoint as a mapping — `{}` for anything else."""
    return item if isinstance(item, Mapping) else {}


def rows(values: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    """A list-valued field of the checkpoint, entry by entry.

    A string is not a list of rows — iterating one would yield characters — so the
    `str | bytes` exclusion is load-bearing rather than defensive noise.
    """
    value = values.get(key)
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return []
    return [mapping(item) for item in value]


def number(value: Any) -> float | None:
    """A checkpoint field as a number, or None when it is anything else.

    `bool` is excluded because it *is* an int in Python, and a threshold reported
    as `True` would render as ">= 1". Everything that compares a checkpoint value
    against a threshold goes through this: an unnarrowed `Any` reaching a `>=`
    raises `TypeError` on a string, which on a read-only endpoint is a 500 handed
    to a reader for a row they did not write.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)
