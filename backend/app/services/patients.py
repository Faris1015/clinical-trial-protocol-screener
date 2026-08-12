"""The synthetic cohort as a first-class collection (#96).

Until now the EHR was an input the Matcher read and nobody else could see: a
patient existed only as a row in the cohort table of whichever run happened to
score them, and `PT-0001` outside that table meant nothing. This module makes the
cohort readable on its own terms, which is what the reverse question — *which
protocols does this patient qualify for?* — needs before it can be asked at all.

The file is the authority, read through `matcher.load_patients` rather than
re-opened here: it is the same records the Matcher scored, and a second reader
with its own error handling would be a second answer to "what is in the EHR".

**Nothing here is cached.** The obvious `lru_cache` would make a patient list a
free read, and it would also mean a regenerated EHR is invisible until the
process restarts — on a demo deployment where regenerating it is a documented
step, that is a stale page presented as a live one. The file is a hundred records
and the endpoints that read it are rate-limited on the read bucket, so the cost
of being correct here is a JSON parse.

Summaries, not records, are what the index returns. A list of a hundred patients
carrying every lab, diagnosis and medication would be most of the EHR shipped to
render a table of names — and the record is one click away on the detail route,
which is where a reader who wants it is going anyway.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, TypedDict

from app.exceptions import PatientNotFoundError
from app.graph.nodes import matcher

# The page bounds are the route's (`main.DEFAULT_PAGE_SIZE`/`MAX_PAGE_SIZE`),
# declared on the query parameters so an over-large `limit` is a 422 before a
# handler runs — the same two constants the runs index and the audit log are
# capped by. Restating them here would be a second ceiling that could differ from
# the one actually enforced.


class PatientSummary(TypedDict):
    """One row of the cohort index.

    Carries the counts rather than the lists: the index is a table of who exists,
    and "3 diagnoses" is what a reader scans by. `age` is lifted out of `labs`
    because it is the one lab value that reads as demographics — every other one
    is a measurement that only means something beside a criterion.
    """

    id: str
    name: str
    sex: str
    cohort: str
    age: float | None
    diagnoses: int
    medications: int
    history: int


def _count(record: Mapping[str, Any], field: str) -> int:
    value = record.get(field)
    # Defensive for the same reason `services/checkpoint.py` is: the EHR is a
    # JSON file a demo deployment regenerates, and a hand-edited record must
    # render as a patient with nothing on file rather than 500 an index page.
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return 0
    return len(value)


def summarize(record: Mapping[str, Any]) -> PatientSummary:
    labs = record.get("labs")
    age = labs.get("age") if isinstance(labs, Mapping) else None
    return PatientSummary(
        id=str(record.get("id") or ""),
        name=str(record.get("name") or ""),
        sex=str(record.get("sex") or ""),
        cohort=str(record.get("cohort") or ""),
        age=float(age) if isinstance(age, int | float) and not isinstance(age, bool) else None,
        diagnoses=_count(record, "diagnoses"),
        medications=_count(record, "medications"),
        history=_count(record, "history"),
    )


def _matches(record: Mapping[str, Any], needle: str) -> bool:
    """Case-insensitive substring match on id or name — what the runs index does
    for filename or thread id, and the same two things a reader has to hand."""
    return needle in str(record.get("id") or "").lower() or (
        needle in str(record.get("name") or "").lower()
    )


def list_patients(*, limit: int, offset: int = 0, search: str | None = None) -> dict:
    """One page of the cohort, in the order the generator wrote it.

    Generator order, not sorted: the ids are sequential (`PT-0001`…), so it *is*
    id order, and re-sorting would only make the two disagree if a record were
    ever added out of band.

    Returns the same `{items, total, limit, offset}` envelope as the runs index
    and the audit log — `total` counts the whole filtered cohort, not the page, so
    the caller can say whether a next page exists.
    """
    records = matcher.load_patients()
    if search:
        needle = search.strip().lower()
        records = [r for r in records if _matches(r, needle)]
    page = records[offset : offset + limit]
    return {
        "items": [summarize(r) for r in page],
        "total": len(records),
        "limit": limit,
        "offset": offset,
    }


def get_patient(patient_id: str) -> dict:
    """One patient's whole record — labs, diagnoses, medications, history.

    Returned verbatim rather than reshaped into a schema of this module's own: it
    is the record the Matcher scored, and a view that reformatted it could show a
    reader something subtly different from what the verdicts beside it were
    reached from.
    """
    for record in matcher.load_patients():
        if record.get("id") == patient_id:
            return dict(record)
    raise PatientNotFoundError(
        f"No patient {patient_id} is in the synthetic cohort. If the records were "
        "regenerated, ids from an earlier cohort no longer resolve."
    )
