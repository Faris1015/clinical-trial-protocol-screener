"""The machine-readable cohort export (#102) — one run as CSV or JSON.

`services/report.py` produces the artifact an *auditor* reads: a printable
document, prose beside tables, self-contained in a browser. A coordinator's next
step is different work — loading candidate patients into a CTMS or a spreadsheet
— and no amount of HTML makes that possible. This module is the other half of the
same snapshot: the same `/state` payload, the same buckets, rendered for a machine.

Five decisions worth knowing before editing this module:

**One derivation of the bucket, not a second one.** Which bucket a patient is in
comes from `services/cohort.py`, exactly as the report, the runs index's match
count and the comparison take it. An export that said "eligible" where the report
said "needs review" would be worse than no export — a coordinator would enroll
against it. That is AC 3, and it is satisfied structurally: there is no bucket
rule in this file.

**The CSV is wide — one row per patient.** The reader is a coordinator with a
spreadsheet, and the row they want to filter, sort and paste is a patient. So the
fixed columns are the patient and their verdict, and every criterion in the
extraction gets a column after them carrying that patient's status for it. The
long form (one row per patient-criterion) is the better *audit* shape and it is
what the JSON export gives; a CSV in that shape would need a pivot before anyone
could use it for the thing this issue exists for.

**Criteria are identified by `attrition.criterion_key`.** The same key the
attrition panel ranks by, the coverage score counts and the what-if simulator
takes an override on. Two criteria that render identically are one column, which
is the same merge every other view makes — and a criterion the cohort carries a
verdict for that the extraction no longer lists still gets a column, appended
after the extraction's own, rather than being dropped: an old checkpoint must not
lose verdicts silently.

**Excel is a formula engine, and this data is untrusted.** Every label and
sentence in the export came out of an uploaded document by way of an LLM. A cell
beginning `=`, `+`, `-`, `@`, tab or CR is executed as a formula on open in both
Excel and Sheets, so `_csv_cell` prefixes those with an apostrophe — the one
mitigation both applications honor. Numbers are exempted, so a legitimate `-3.2`
stays a number rather than becoming text. This is the same class of decision as
`report._esc`: applied at every cell, with no other way to build a row.

**JSON is the self-describing one.** It carries the approved criteria with their
`source_text`, who authorized the matching, and the run's identity alongside the
cohort — so an export handed to an external auditor is readable without the app
that produced it and without the report beside it (AC 2). The CSV cannot carry
that nesting; its header row names the criteria, and the JSON is the artifact for
anyone who needs the provenance too.

Like the report, this **does** carry patient data — that is what it is for. It is
authenticated at the same rung, served as an attachment, and the download is
attributed in the log by `services/screening.get_screening_export`.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, NamedTuple

from app.services import cohort
from app.services.attrition import criterion_key
from app.services.checkpoint import mapping, rows
from app.services.criteria_edits import BUCKET_KINDS, UNPARSEABLE_BUCKET, criterion_label
from app.services.report import DISCLAIMER, has_reportable_content
from app.services.uploads import sanitize_filename

# The fixed columns every CSV row opens with, before the per-criterion ones. Order
# is part of the contract (AC 5): a coordinator with a saved import mapping must
# not have their columns move under them between two runs of the same protocol.
_PATIENT_COLUMNS = ("patient_id", "patient_name", "bucket", "bucket_label", "assessment")

# Excel and Sheets both evaluate a cell that opens with one of these. Tab and CR
# are here because they can smuggle a leading `=` past a naive check.
_FORMULA_LEADERS = ("=", "+", "-", "@", "\t", "\r")

# Written as the first characters of the CSV body. Without it Excel on Windows
# reads a UTF-8 file as the system codepage and a protocol's "≥" or an accented
# patient name arrives mojibake — the file "opens", but wrongly, which is worse
# than failing to open (AC 5).
#
# Spelled as an escape rather than as the literal character: U+FEFF is
# zero-width, and a reader of this source could not tell a present one from an
# absent one, nor an editor be trusted not to strip it.
_BOM = "\ufeff"

# What both exports say for a criterion the run holds no verdict on for a patient.
# Empty would read as "passed silently" in a spreadsheet column of `pass`, and as
# an ambiguous null to a JSON consumer; one explicit word in both is what keeps the
# two files from describing the same gap two ways. `services/coverage.py` counts
# exactly this case as unresolved.
_NO_RESULT = "not evaluated"


def _text(value: Any) -> str:
    """A checkpoint value as a string, with `None` empty rather than "None"."""
    return "" if value is None else str(value)


def _strings(values: Mapping[str, Any], key: str) -> list[str]:
    """A list-valued field whose entries are sentences rather than mappings.

    `checkpoint.rows` narrows each entry to a mapping, which is right for every
    other list on a checkpoint and wrong for `unparseable` — it holds the verbatim
    protocol sentences, so narrowing them would return a list of empty dicts.
    """
    value = values.get(key)
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return []
    return [_text(entry) for entry in value]


def _csv_cell(value: Any) -> str:
    """One cell, defused against spreadsheet formula injection.

    See the module docstring. The apostrophe is data — a reader who opens the file
    in a text editor sees it — which is the accepted cost of the only mitigation
    Excel and Sheets both honor. Numeric strings are exempted so `-3.2` survives as
    a number; the check is on the rendered text rather than the source type because
    an LLM-supplied threshold can arrive as either.
    """
    text = _text(value)
    if not text.startswith(_FORMULA_LEADERS):
        return text
    try:
        float(text)
    except ValueError:
        return f"'{text}"
    return text


class _Criterion:
    """One column of the export: a criterion, its identity, and its provenance."""

    def __init__(self, key: str, kind: str, bucket: str, criterion: Mapping[str, Any]) -> None:
        self.key = key
        self.kind = kind
        self.bucket = bucket
        self.label = criterion_label(criterion)
        self.source_text = _text(criterion.get("source_text"))


def _extraction_criteria(values: Mapping[str, Any]) -> dict[str, _Criterion]:
    """Every structured criterion in the approved extraction, keyed, in bucket order.

    Insertion order is the column order, and it is the report's reading order —
    inclusion before exclusion, numeric before categorical — so a reader holding
    both artifacts walks the criteria in one sequence. `unparseable` is not here:
    it holds sentences the Parser declined to structure, which no patient was ever
    scored against, and a column of "not evaluated" for all of them would imply the
    Matcher tried. They travel on the JSON export as their own list instead.
    """
    criteria = mapping(values.get("parsed_criteria"))
    columns: dict[str, _Criterion] = {}
    for bucket, kind in BUCKET_KINDS.items():
        for item in rows(criteria, bucket):
            if not item:
                continue
            key = criterion_key(kind, item)
            # First wins: two criteria that render identically are one column, the
            # same merge `services/attrition.py` makes for the same reason.
            columns.setdefault(key, _Criterion(key, kind, bucket, item))
    return columns


class _Layout(NamedTuple):
    """The export's columns, and how a cohort result row finds the one it belongs to.

    Built once per rendered file and threaded through, because the two have to be
    derived together: a result row resolved to a key the column list does not carry
    is a verdict that reaches no cell.
    """

    columns: list[_Criterion]
    # Column key by rendered label, for the one case a result row cannot be keyed
    # directly — see `_result_key`.
    by_label: dict[str, str]


def _layout(values: Mapping[str, Any], evaluations: Sequence[Mapping[str, Any]]) -> _Layout:
    """Every column this export carries, in order, with its label index.

    The extraction's own criteria first, in the report's reading order. Then any
    criterion the *cohort* was scored against that the extraction no longer lists —
    empty on every run this build produces (the checkpoint holding the cohort holds
    the criteria it was scored against), but not for a checkpoint written by an
    older build or hand-edited in a demo. Those are appended rather than dropped: a
    verdict silently missing from an export is precisely the failure this module
    exists to make impossible, and appending keeps the stable prefix stable.
    """
    known = _extraction_criteria(values)
    layout = _Layout(columns=list(known.values()), by_label={})
    for column in layout.columns:
        layout.by_label.setdefault(column.label, column.key)

    for evaluation in evaluations:
        for result in rows(evaluation, "criterion_results"):
            criterion = mapping(result.get("criterion"))
            if not criterion:
                continue
            key = _result_key(result, criterion, layout.by_label)
            if key in known:
                continue
            known[key] = _Criterion(key, _text(result.get("kind")), "", criterion)
            layout.columns.append(known[key])
            layout.by_label.setdefault(known[key].label, key)
    return layout


def _result_key(
    result: Mapping[str, Any], criterion: Mapping[str, Any], by_label: Mapping[str, str]
) -> str:
    """Which column a cohort result row belongs to.

    Normally `attrition.criterion_key` over the row's own `kind` — the identity the
    attrition panel, the coverage score and the what-if simulator all address a
    criterion by.

    The fallback covers a result row carrying no `kind` at all, which is the same
    old-or-hand-edited checkpoint this module's extra columns exist for. Keying it
    directly would mint `":age >= 18 years"` beside the extraction's
    `"inclusion:age >= 18 years"` — two columns under one identical header, with the
    verdict in the second and "not evaluated" in the first, which is worse than
    either alone. Matching on the rendered label folds it into the column it plainly
    belongs to.
    """
    kind = _text(result.get("kind"))
    if kind:
        return criterion_key(kind, criterion)
    return by_label.get(criterion_label(criterion)) or criterion_key(kind, criterion)


# How a verdict ranks when one criterion was applied to one patient twice, worst
# winning. Two orderings matter and they are not the same one:
#
#   - A status this build cannot read outranks `pass` and `unknown`. It is
#     something we could not interpret, and quietly letting it lose to a `pass`
#     would hide it — the reading `services/coverage.py` takes of the same statuses.
#   - `fail` outranks it in turn. A `fail` is a verdict the Matcher actually
#     recorded, and a definite exclusion must not be masked by an unreadable row
#     beside it: the cell a coordinator filters on has to show the failure.
_SEVERITY = {"pass": 0, "unknown": 1, "fail": 3}
_UNREADABLE_SEVERITY = 2


class _Verdict(NamedTuple):
    """One patient's settled answer for one criterion."""

    status: str
    explanation: str


# The answer for a criterion this patient carries no result row for at all — and
# for a row that carries no status, which says exactly as much.
_ABSENT = _Verdict(status=_NO_RESULT, explanation="")


def _verdicts(evaluation: Mapping[str, Any], by_label: Mapping[str, str]) -> dict[str, _Verdict]:
    """This patient's verdict per column key, worst status winning.

    A duplicated criterion contributes one verdict, and the pessimistic one: the
    export's cell for a criterion a patient both passed and failed has to be the
    fail, or a coordinator filters them into the enrollable column. The explanation
    is carried alongside rather than resolved separately, so the sentence always
    describes the status printed beside it.
    """
    ranked: dict[str, tuple[int, _Verdict]] = {}
    for result in rows(evaluation, "criterion_results"):
        criterion = mapping(result.get("criterion"))
        if not criterion:
            # A result row carrying no criterion — a null in a hand-edited
            # checkpoint. There is no column to attribute it to; `services/
            # attrition.py` drops it for the same reason.
            continue
        key = _result_key(result, criterion, by_label)
        status = _text(result.get("status"))
        # A row with no status at all is not an unreadable verdict, it is an absent
        # one, and it has to *say* "not evaluated" rather than render as a blank
        # cell — a blank in a column of `pass` reads as a silent pass.
        verdict = _Verdict(status, _text(result.get("explanation"))) if status else _ABSENT
        rank = _SEVERITY.get(status, _UNREADABLE_SEVERITY)
        previous = ranked.get(key)
        if previous is None or rank > previous[0]:
            ranked[key] = (rank, verdict)
    return {key: verdict for key, (_rank, verdict) in ranked.items()}


# --- CSV --------------------------------------------------------------------


def render_csv(payload: Mapping[str, Any]) -> str:
    """The cohort as a spreadsheet: one row per patient, one column per criterion.

    A run with no cohort renders as a header row and nothing under it. That is the
    honest answer — the criteria are the header, so the file still says what this
    run screens on — and it keeps the endpoint's shape independent of how far the
    run got, which a script polling for an export depends on.
    """
    values = mapping(payload.get("values"))
    evaluations = rows(values, "matched_patients")
    layout = _layout(values, evaluations)
    columns = layout.columns

    # `\r\n` is RFC 4180's terminator and what Excel expects; `csv` quotes any cell
    # containing a comma, quote or newline, which is the whole of AC 5's "quoted
    # free text" — a protocol sentence with a comma in it must not split a row.
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow([*_PATIENT_COLUMNS, *(_csv_cell(column.label) for column in columns)])
    for evaluation in evaluations:
        bucket = cohort.bucket_of(evaluation)
        verdicts = _verdicts(evaluation, layout.by_label)
        writer.writerow(
            [
                _csv_cell(evaluation.get("patient_id")),
                _csv_cell(evaluation.get("name")),
                bucket,
                cohort.BUCKET_LABELS.get(bucket, bucket),
                _csv_cell(evaluation.get("summary")),
                *(_csv_cell(verdicts.get(column.key, _ABSENT).status) for column in columns),
            ]
        )
    return _BOM + buffer.getvalue()


# --- JSON -------------------------------------------------------------------


def build_export(
    payload: Mapping[str, Any], *, generated_at: datetime | None = None
) -> dict[str, Any]:
    """The cohort as a self-describing document (AC 2).

    Everything needed to read the cohort without the app: the run's identity, the
    approved criteria with the protocol sentence each came from, the sentences that
    never became criteria, who authorized the matching, and then the patients with
    their per-criterion verdicts in long form. The disclaimer travels with it for
    the same reason the report carries one — this file outlives the context that
    makes "synthetic data" obvious.
    """
    values = mapping(payload.get("values"))
    record = mapping(payload.get("screening"))
    criteria = mapping(values.get("parsed_criteria"))
    evaluations = rows(values, "matched_patients")
    layout = _layout(values, evaluations)
    stamped = generated_at or datetime.now(UTC)

    return {
        "disclaimer": DISCLAIMER,
        "run": {
            "thread_id": _text(record.get("thread_id")),
            "source_filename": _text(
                record.get("source_filename") or values.get("source_filename")
            ),
            "trial_title": _text(criteria.get("trial_title")),
            "status": _text(record.get("status") or values.get("current_step")),
            "created_at": _text(record.get("created_at")),
            "criteria_revision": values.get("criteria_revision"),
            "exported_at": stamped.astimezone(UTC).isoformat(),
        },
        # Present even when nobody cleared the gate, with nulls: an auditor reading
        # an export of a parked run needs "no approval recorded" stated, not
        # inferred from a key that isn't there.
        "authorization": {
            "approved_by": values.get("approved_by"),
            "approved_by_role": values.get("approved_by_role"),
            "approved_at": values.get("approved_at"),
        },
        "criteria": [
            {
                "key": column.key,
                "bucket": column.bucket,
                "kind": column.kind,
                "label": column.label,
                "source_text": column.source_text,
            }
            for column in layout.columns
        ],
        # The sentences nobody screened on. On the export for the same reason the
        # report prints them: they are the reviewer's check-by-hand list, and an
        # export that omitted them would present a partial screen as a whole one.
        "unparseable": _strings(criteria, UNPARSEABLE_BUCKET),
        "counts": cohort.bucket_counts(evaluations),
        "patients": [_patient(evaluation, layout) for evaluation in evaluations],
    }


def _patient(evaluation: Mapping[str, Any], layout: _Layout) -> dict[str, Any]:
    """One patient: their bucket, the matcher's sentence, and every verdict.

    Long form rather than the CSV's wide one — a nested list is the shape a machine
    consuming JSON wants, and it can carry the explanation behind each verdict,
    which is the sentence a coordinator quotes when a site asks why someone was
    screened out. Every criterion appears, including the ones this patient has no
    result for, so two patients' `results` lists are the same length and index.
    """
    bucket = cohort.bucket_of(evaluation)
    verdicts = _verdicts(evaluation, layout.by_label)
    return {
        "patient_id": _text(evaluation.get("patient_id")),
        "name": _text(evaluation.get("name")),
        "bucket": bucket,
        "bucket_label": cohort.BUCKET_LABELS.get(bucket, bucket),
        "summary": _text(evaluation.get("summary")),
        "results": [
            _result(column, verdicts.get(column.key, _ABSENT)) for column in layout.columns
        ],
    }


def _result(column: _Criterion, verdict: _Verdict) -> dict[str, Any]:
    """One cell of the long form: the criterion, restated, and this patient's answer.

    The criterion's label and provenance are repeated on every result rather than
    referenced by `criterion_key` alone. It costs bytes and buys the property the
    issue asks for — a reader can answer "why was this patient excluded" from the
    result row in hand, without joining it back against the `criteria` list.
    """
    return {
        "criterion_key": column.key,
        "label": column.label,
        "kind": column.kind,
        "source_text": column.source_text,
        "status": verdict.status,
        "explanation": verdict.explanation,
    }


# --- Download mechanics -----------------------------------------------------


def export_filename(payload: Mapping[str, Any], fmt: str) -> str:
    """`trialgate-cohort-<protocol>-<run>.<fmt>` — the report's rule, one noun over.

    Same construction as `report.report_filename` (sanitized protocol stem plus the
    run id's first block) so a reviewer's download folder sorts the two artifacts
    of one run together, and re-sanitized here for the same reason: this value is
    interpolated into a `Content-Disposition` header, where a quote or a newline
    would be header injection rather than a cosmetic bug.
    """
    record = mapping(payload.get("screening"))
    values = mapping(payload.get("values"))
    stem = sanitize_filename(
        _text(record.get("source_filename") or values.get("source_filename") or "screening")
    ).rsplit(".", 1)[0]
    run = sanitize_filename(_text(record.get("thread_id")))[:8]
    parts = [part for part in ("trialgate-cohort", stem or "screening", run) if part]
    return f"{'-'.join(parts)}.{fmt}"


def has_exportable_content(payload: Mapping[str, Any]) -> bool:
    """Whether this run produced anything an export could be about.

    The report's own test, deliberately: a screening uploaded but never streamed has
    no checkpoint and therefore no criteria to name in a header row and no cohort
    under it, and the two downloads sitting beside each other in the UI must refuse
    the same runs. A run that parsed but never matched is *allowed* — its export is
    the criteria with an empty cohort, which is a true statement about the run.
    """
    return has_reportable_content(payload)
