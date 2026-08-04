"""The downloadable screening report (#56) — one self-contained HTML document.

This is the artifact a reviewer hands off: extracted criteria beside the verbatim
protocol sentences they came from, the reviewer revisions applied to them, the
Critic's findings in both layers, who authorized patient matching, the cohort, and
the execution log — for one run, dated, branded, and readable with nothing but a
browser.

Four decisions worth knowing before editing this module:

**HTML, not PDF — and printable.** A real PDF pipeline means WeasyPrint (Cairo,
Pango, system font packages in both container images) or a headless browser, for
an artifact whose readers already have one. So the document is HTML with print
styles: `@page` margins, repeated table headers, no page break inside a row. A
reviewer who needs a PDF prints to one and gets the same layout, and the acceptance
criterion's "PDF **or** HTML" is met without a new native dependency in the image.

**Self-contained is literal.** No stylesheet link, no webfont, no script, no
image — one file that renders identically from a mail attachment, a shared drive,
or an evidence folder five years from now. The styles are inlined in a single
`<style>`; the palette is the light theme's tokens (frontend/src/app/globals.css)
hard-coded, because a report is printed on white.

**Both layers, no toggle.** The app lets a reader choose plain or technical (#52);
a document cannot ask. Findings and verdicts therefore carry *both* — the
plain-language sentence as the body, the rule id / operator / threshold beside it.
A handoff artifact that dropped either would fail a different reader.

**Everything is escaped, exactly once.** Every string here is downstream of an
uploaded document and an LLM: criteria text, source sentences, the trial title,
the filename. `_esc` is applied at each interpolation and the module builds no
markup any other way, so a protocol sentence containing `<script>` is text in the
report rather than script in a page served from the app's own origin. The route
adds the belt-and-braces (attachment disposition, nosniff, a locked-down CSP).

Unlike a notification (services/notifications.py), this document **does** carry
patient data — that is what it is for. It is authenticated, served as an
attachment, and never leaves the process on its own.
"""

from __future__ import annotations

import html
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from app.services import cohort, timeline
from app.services.criteria_edits import criterion_label
from app.services.uploads import sanitize_filename

# The criteria buckets the report walks, in reading order, with the heading each
# gets. `unparseable` is rendered separately (it holds sentences, not criteria).
_CRITERIA_SECTIONS = (
    ("inclusion_quantitative", "Inclusion · numeric"),
    ("inclusion_categorical", "Inclusion · categorical"),
    ("exclusion_quantitative", "Exclusion · numeric"),
    ("exclusion_categorical", "Exclusion · categorical"),
)

# Mirrors the frontend's `statusLabel` (frontend/src/lib/runs.ts) over the
# backend's own `ScreeningStatus` (app/graph/state.py). Duplicated rather than
# imported across the stack boundary — but it must stay in step: a report and the
# run detail view it was exported from cannot name the same phase differently.
_PHASE_LABELS = {
    "routing": "Routing",
    "parsing": "Parsing",
    "critiquing": "Critiquing",
    "awaiting_approval": "Awaiting approval",
    "matching": "Matching",
    "done": "Done",
    "failed": "Failed",
    "escalated": "Escalated",
}

# What each non-final phase means for the report's completeness. A reader holding
# a report for a parked run has to be told the cohort is absent *because the run
# has not reached matching* — not because nobody matched.
_PHASE_NOTES = {
    "awaiting_approval": (
        "This run is parked at the approval gate: the criteria below have passed "
        "compliance review, but no patient data has been matched yet."
    ),
    "escalated": (
        "The Critic could not converge on a compliant extraction for this run, so it "
        "was escalated for human review and no patients were matched."
    ),
    "failed": (
        "This run did not finish. Whatever it produced before failing is reported "
        "below; the cohort is absent because matching never ran."
    ),
    "routing": "This run had not progressed past intake when the report was taken.",
    "parsing": "This run was still extracting criteria when the report was taken.",
    "critiquing": "This run was still under compliance review when the report was taken.",
    "matching": "This run was still matching patients when the report was taken.",
}

# Carried on every report, at the top and in the footer. The demo screens
# generated patients (app/data/generate_ehr.py) against a simplified rule set, and
# an exported document outlives the context that makes that obvious — it is the one
# artifact here that can end up in an inbox with no app around it.
DISCLAIMER = (
    "Synthetic data — not for clinical use. Every patient record in this report is "
    "generated, and the compliance rules applied are a simplified demonstration set. "
    "Nothing here describes a real person or constitutes a regulatory review."
)

_STYLES = """
/* Top level, not inside the print block below: `@page` only applies when the
 * document is paginated anyway, and a page rule nested in a conditional group is
 * the form browsers have been least consistent about honoring. */
@page { margin: 16mm; }
:root {
  --ink: #131a2b;
  --muted: #5b6780;
  --line: #dde4ee;
  --ground: #f7f9fc;
  --brand: #2f6fe4;
  --pass: #1e7f4f;
  --fail: #c33d39;
  --warn: #a86a12;
}
* { box-sizing: border-box; }
body {
  margin: 0 auto;
  max-width: 60rem;
  padding: 2rem 1.5rem 3rem;
  color: var(--ink);
  background: #fff;
  font: 13px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, Roboto,
    "Helvetica Neue", Arial, sans-serif;
}
h1 { font-size: 1.45rem; margin: 0 0 .25rem; letter-spacing: -.01em; }
h2 {
  font-size: 1rem;
  margin: 2rem 0 .6rem;
  padding-bottom: .3rem;
  border-bottom: 1px solid var(--line);
}
h3 { font-size: .8rem; margin: 1.1rem 0 .35rem; color: var(--muted); text-transform: uppercase;
     letter-spacing: .06em; }
p { margin: .4rem 0; }
code, .mono { font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
  font-size: .92em; }
.brand { display: flex; align-items: center; gap: .5rem; font-weight: 600; color: var(--brand);
  letter-spacing: -.01em; }
.brand-mark {
  display: inline-flex; align-items: center; justify-content: center;
  width: 1.35rem; height: 1.35rem; border-radius: .3rem;
  background: var(--brand); color: #fff; font-size: .8rem; font-weight: 700;
}
.masthead { border-bottom: 2px solid var(--brand); padding-bottom: .9rem; }
.subtitle { color: var(--muted); margin: 0; }
.meta { display: grid; grid-template-columns: max-content 1fr; gap: .15rem 1rem; margin: 1rem 0 0; }
.meta dt { color: var(--muted); }
.meta dd { margin: 0; }
.disclaimer {
  margin: 1.2rem 0 0; padding: .6rem .8rem; border-radius: .35rem;
  border: 1px solid #e6c98f; background: #fdf6e7; color: #6b4a05;
}
.note { color: var(--muted); background: var(--ground); border: 1px solid var(--line);
  border-radius: .35rem; padding: .6rem .8rem; }
table { width: 100%; border-collapse: collapse; margin: .4rem 0 1rem; }
table.split { table-layout: fixed; }
table.split td, table.split th { word-wrap: break-word; }
table.split th:first-child { width: 38%; }
th, td { text-align: left; vertical-align: top; padding: .35rem .5rem;
  border-bottom: 1px solid var(--line); }
th { background: var(--ground); font-size: .78rem; text-transform: uppercase;
  letter-spacing: .05em; color: var(--muted); font-weight: 600; }
td.provenance { color: var(--muted); }
.tag { display: inline-block; padding: 0 .4rem; border-radius: .25rem; font-size: .75rem;
  font-weight: 600; border: 1px solid currentColor; white-space: nowrap; }
.tag-pass { color: var(--pass); }
.tag-fail { color: var(--fail); }
.tag-warn { color: var(--warn); }
.tag-plain { color: var(--muted); }
.counts { display: flex; flex-wrap: wrap; gap: .4rem; margin: .4rem 0 .8rem; }
.detail { color: var(--muted); margin: .2rem 0 0; }
.stamp { color: var(--muted); border-top: 1px solid var(--line); margin-top: 2.5rem;
  padding-top: .8rem; font-size: .8rem; }
@media print {
  body { max-width: none; padding: 0; font-size: 10.5pt; }
  h2 { break-after: avoid; }
  tr, .note, .disclaimer { break-inside: avoid; }
  /* Repeat the header row when a cohort table runs over a page. */
  thead { display: table-header-group; }
}
"""


def _esc(value: Any) -> str:
    """Every interpolation into this document goes through here.

    `quote=True` (the default) so the same helper is safe in an attribute, and
    `None` renders empty rather than as the string "None".
    """
    return "" if value is None else html.escape(str(value), quote=True)


def _items(values: Mapping[str, Any], key: str) -> list[Any]:
    """A list-valued field of the checkpoint, defensively.

    The report renders whatever a checkpoint holds — including one written by an
    older build of the pipeline, or a run that failed partway — so a field that is
    absent, null, or (after a hand-edited payload) not a list must degrade to an
    empty section rather than a 500 on a download.
    """
    value = values.get(key)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return list(value)
    return []


def _mapping(item: Any) -> Mapping[str, Any]:
    """One row of such a list, guarded the same way."""
    return item if isinstance(item, Mapping) else {}


def _phase(payload: Mapping[str, Any]) -> str:
    """The run's phase, resolved exactly as the run detail view resolves it.

    `pending` wins (a parked run is at the gate whatever else is recorded), then
    the store row, then the checkpoint — a screening uploaded but never streamed
    has no checkpoint at all, so reading `current_step` first would report it as
    finished. See frontend/src/components/runs/run-detail.tsx, which derives the
    same value from the same payload.
    """
    if payload.get("pending"):
        return "awaiting_approval"
    record = _mapping(payload.get("screening"))
    values = _mapping(payload.get("values"))
    return str(record.get("status") or values.get("current_step") or "")


# Which cohort bucket a patient lands in, and how it reads, come from
# services/cohort.py — the report, the runs index's match count and the run
# comparison (#59) must not disagree about who was eligible. Only the tag colour is
# this document's own business.
_BUCKET_TAGS = {"eligible": "pass", "review": "warn", "ineligible": "fail"}


def _tag(text: str, variant: str) -> str:
    return f'<span class="tag tag-{_esc(variant)}">{_esc(text)}</span>'


def _timestamp(iso: Any) -> str:
    """An ISO-8601 instant as a fixed, unambiguous UTC stamp.

    UTC and explicit, not the exporting server's locale: this document is read
    somewhere else, possibly years later, and "07/03/2026 4:12 PM" with no zone is
    a worse record than none. A malformed value renders as itself.
    """
    if not iso:
        return ""
    try:
        parsed = datetime.fromisoformat(str(iso))
    except ValueError:
        return str(iso)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _rows(rows: Iterable[str]) -> str:
    return "\n".join(rows)


def _table(headers: Sequence[str], rows: Sequence[str], *, css_class: str = "") -> str:
    """A table, or nothing at all when there are no rows.

    Callers pass row markup they built with `_esc`; the headers are literals from
    this module. Returning "" for an empty body is what lets every section below
    compose without each one repeating an emptiness check.
    """
    if not rows:
        return ""
    head = "".join(f"<th>{_esc(h)}</th>" for h in headers)
    attr = f' class="{_esc(css_class)}"' if css_class else ""
    return f"<table{attr}><thead><tr>{head}</tr></thead><tbody>{_rows(rows)}</tbody></table>"


def _section(title: str, body: str, *, region: str) -> str:
    """A titled section, dropped entirely when its body is empty."""
    if not body.strip():
        return ""
    return f'<section data-region="{_esc(region)}"><h2>{_esc(title)}</h2>{body}</section>'


# --- Sections ---------------------------------------------------------------


def _masthead(payload: Mapping[str, Any], generated_at: datetime) -> str:
    values = _mapping(payload.get("values"))
    record = _mapping(payload.get("screening"))
    criteria = _mapping(values.get("parsed_criteria"))
    phase = _phase(payload)
    filename = record.get("source_filename") or values.get("source_filename") or "Screening"
    fields = [
        ("Protocol", _esc(filename)),
        ("Trial", _esc(criteria.get("trial_title")) or "—"),
        ("Phase", _esc(_PHASE_LABELS.get(phase, phase)) or "—"),
        ("Run id", f'<span class="mono">{_esc(record.get("thread_id"))}</span>'),
        ("Uploaded", _esc(_timestamp(record.get("created_at"))) or "—"),
        ("Generated", _esc(_timestamp(generated_at))),
    ]
    revision = values.get("criteria_revision")
    if revision:
        # Only when a human has revised the extraction — "revision 0" would read
        # like a version number on every other report rather than a fact about
        # this one.
        fields.append(("Criteria revision", _esc(revision)))
    meta = "".join(f"<dt>{_esc(label)}</dt><dd>{value}</dd>" for label, value in fields)
    return f"""<header class="masthead">
  <div class="brand"><span class="brand-mark">TG</span>TrialGate</div>
  <h1>Screening report</h1>
  <p class="subtitle">Extracted eligibility criteria, compliance review, and cohort match.</p>
  <dl class="meta">{meta}</dl>
</header>
<p class="disclaimer">{_esc(DISCLAIMER)}</p>"""


def _phase_note(payload: Mapping[str, Any]) -> str:
    note = _PHASE_NOTES.get(_phase(payload))
    return f'<p class="note" data-region="phase-note">{_esc(note)}</p>' if note else ""


def _criteria_section(values: Mapping[str, Any]) -> str:
    """The extraction, each criterion beside the sentence it was extracted from.

    Provenance is a column rather than a tooltip (the app's own affordance,
    CriteriaTable): a printed page has no hover, and the verbatim sentence is the
    half of this table an auditor actually checks.
    """
    criteria = _mapping(values.get("parsed_criteria"))
    if not criteria:
        return ""
    blocks: list[str] = []
    for bucket, heading in _CRITERIA_SECTIONS:
        rows = [
            f"<tr><td>{_esc(criterion_label(item))}</td>"
            f'<td class="provenance">{_esc(item.get("source_text")) or "—"}</td></tr>'
            for item in (_mapping(entry) for entry in _items(criteria, bucket))
            if item
        ]
        # `split` fixes the criterion/provenance ratio across the four bucket
        # tables: auto layout sizes each one to its own longest cell, so a bucket
        # of short criteria puts its provenance column somewhere different from
        # the bucket above it and the eye loses the column.
        table = _table(("Criterion", "Verbatim protocol text"), rows, css_class="split")
        if table:
            blocks.append(f"<h3>{_esc(heading)}</h3>{table}")

    unparseable = [str(entry) for entry in _items(criteria, "unparseable")]
    if unparseable:
        # Reported, not hidden: a sentence the Parser refused to invent numbers for
        # is a criterion nobody screened on, which is exactly what a reviewer
        # reading this report needs to know it has to check by hand.
        rows = [f"<tr><td>{_esc(sentence)}</td></tr>" for sentence in unparseable]
        blocks.append(
            "<h3>Not converted to structured criteria</h3>"
            + _table(("Verbatim protocol text",), rows)
        )
    return _section("Extracted criteria", "".join(blocks), region="report-criteria")


def _edits_section(values: Mapping[str, Any]) -> str:
    """Every reviewer revision of the extraction, oldest first (#53).

    The criteria above are the *current* revision. If a human changed them, the
    report has to say who and what — the cohort below was scored against their
    edit, not the Parser's output.
    """
    rows: list[str] = []
    for entry in (_mapping(item) for item in _items(values, "criteria_edits")):
        who = " ".join(
            part
            for part in (
                _esc(entry.get("edited_by")),
                f"({_esc(entry.get('edited_by_role'))})" if entry.get("edited_by_role") else "",
            )
            if part
        )
        for change in (_mapping(item) for item in _items(entry, "changes")):
            before, after = change.get("before"), change.get("after")
            rows.append(
                f"<tr><td>{_esc(entry.get('revision'))}</td>"
                f"<td>{_esc(change.get('kind'))}</td>"
                f"<td>{_esc(before) or '—'}</td>"
                f"<td>{_esc(after) or '—'}</td>"
                f'<td>{who}<div class="detail">{_esc(_timestamp(entry.get("edited_at")))}</div>'
                "</td></tr>"
            )
    return _section(
        "Reviewer revisions",
        _table(("Rev", "Change", "Before", "After", "Edited by"), rows),
        region="report-edits",
    )


def _findings_section(values: Mapping[str, Any]) -> str:
    """The Critic's findings in both layers (#52) — plain sentence, technical detail.

    A blocking finding is `reject`, an advisory one `warn`; the severity is named
    in the reviewer's terms ("Must fix" / "Advisory") with the rule id alongside,
    so the row is both actionable and auditable.
    """
    rows: list[str] = []
    for finding in (_mapping(item) for item in _items(values, "compliance_findings")):
        blocking = finding.get("severity") == "reject"
        # `or`, not a default: an explanation that came back empty is as useless as
        # a missing one, and both fall back to the technical wording — the same
        # fallback the app's own views make for pre-#52 runs.
        plain = finding.get("explanation") or finding.get("message")
        severity = _tag("Must fix", "fail") if blocking else _tag("Advisory", "warn")
        rows.append(
            f"<tr><td>{severity}"
            f'<div class="detail mono">{_esc(finding.get("rule_id"))}</div></td>'
            f"<td>{_esc(plain)}"
            f'<div class="detail">{_esc(finding.get("message"))}</div></td></tr>'
        )
    table = _table(("Severity", "Finding"), rows)
    if not table:
        return ""
    summary = values.get("compliance_summary")
    lead = f"<p>{_esc(summary)}</p>" if summary else ""
    return _section("Compliance findings", lead + table, region="report-findings")


def _authorization_section(values: Mapping[str, Any]) -> str:
    """Who authorized patient matching (#50) — the audit line, verbatim.

    Present only once a named reviewer cleared the gate, which is the only way the
    cohort below can exist.
    """
    approver = values.get("approved_by")
    if not approver:
        return ""
    role = f" ({_esc(values.get('approved_by_role'))})" if values.get("approved_by_role") else ""
    at = _timestamp(values.get("approved_at"))
    when = f" on {_esc(at)}" if at else ""
    return _section(
        "Authorization",
        f"<p>Patient matching authorized by <strong>{_esc(approver)}</strong>{role}{when}.</p>",
        region="report-authorization",
    )


def _cohort_section(values: Mapping[str, Any]) -> str:
    """The match table: every evaluated patient, their verdict, and why.

    Both layers again — the matcher's plain sentence for the patient, then the
    criteria that failed or could not be determined in their technical form with
    the protocol sentence behind each. The whole evaluated cohort is listed, not
    just the eligible bucket: "who was screened out and on what" is the part of a
    screening a site actually gets asked to justify.
    """
    evaluations = [_mapping(item) for item in _items(values, "matched_patients")]
    if not evaluations:
        return ""
    counts = cohort.bucket_counts(evaluations)
    rows: list[str] = []
    for evaluation in evaluations:
        bucket = cohort.bucket_of(evaluation)
        unresolved = [
            result
            for result in (_mapping(item) for item in _items(evaluation, "criterion_results"))
            if result.get("status") != "pass"
        ]
        # Status first, then the criterion, then its provenance: leading with
        # fail/unknown is what makes a column of these scannable, and a categorical
        # label already ends in "(condition)" — a parenthesized status after it
        # reads as part of the criterion.
        detail = "".join(
            f'<div class="detail">{_esc(result.get("status"))} — '
            f"{_esc(criterion_label(_mapping(result.get('criterion'))))} · "
            f"{_esc(_mapping(result.get('criterion')).get('source_text'))}</div>"
            for result in unresolved
        )
        rows.append(
            f'<tr><td><span class="mono">{_esc(evaluation.get("patient_id"))}</span>'
            f'<div class="detail">{_esc(evaluation.get("name"))}</div></td>'
            f"<td>{_tag(cohort.BUCKET_LABELS[bucket], _BUCKET_TAGS[bucket])}</td>"
            f"<td>{_esc(evaluation.get('summary'))}{detail}</td></tr>"
        )
    tally = "".join(
        _tag(f"{counts[bucket]} {cohort.BUCKET_LABELS[bucket].lower()}", _BUCKET_TAGS[bucket])
        for bucket in cohort.BUCKET_ORDER
    )
    summary = values.get("match_summary")
    lead = f'<div class="counts">{tally}</div>' + (f"<p>{_esc(summary)}</p>" if summary else "")
    return _section(
        "Cohort",
        lead + _table(("Patient", "Verdict", "Assessment"), rows),
        region="report-cohort",
    )


# How each step's outcome reads as a tag. `rejected`/`escalated` are the Critic
# pushing work back and `edited` is a reviewer correcting it — neither is a
# failure, and only `failed` is terminal-bad. Mirrors the run detail view's
# `outcomeVariant` (frontend/src/components/runs/run-timeline.tsx).
_OUTCOME_TAGS = {
    "completed": "pass",
    "approved": "pass",
    "failed": "fail",
    "rejected": "warn",
    "escalated": "warn",
    "edited": "warn",
}


def _timeline_lead(summary: timeline.TimelineSummary) -> str:
    """The run's shape in one line, above the log (#55).

    Only the figures that say something: a run that never looped, was never
    edited and never escalated is the normal case and gets none of those clauses
    rather than a row of zeros. The counts are this process's own, but they go
    through `_esc` like every other interpolation — the invariant this module
    relies on is that there are no exceptions to check for.
    """
    parts: list[str] = []
    if summary["duration"]:
        parts.append(f"Ran in {_esc(summary['duration'])}")
    if summary["attempts"] > 1:
        parts.append(f"{_esc(summary['attempts'])} extraction attempts")
    rejections = summary["critic_rejections"]
    if rejections:
        parts.append(f"{_esc(rejections)} Critic rejection{'s' if rejections != 1 else ''}")
    revisions = summary["revisions"]
    if revisions:
        parts.append(f"{_esc(revisions)} reviewer revision{'s' if revisions != 1 else ''}")
    if summary["escalated"]:
        parts.append("escalated for human review")
    return f"<p>{' · '.join(parts)}.</p>" if parts else ""


def _events_section(values: Mapping[str, Any]) -> str:
    """The run's execution timeline, in order — who did what, when (#55).

    Built by `services.timeline` rather than walked here, so the document and the
    run detail view are two renderings of one derivation: the attempt numbers, the
    elapsed gaps and the reviewer identities all come out the same in both.
    """
    trail = timeline.build_timeline(values)
    # Number the retry rounds only for a run that actually looped: on the common
    # single-attempt run "attempt 1" on two rows is noise, not provenance.
    numbered = trail["summary"]["attempts"] > 1
    rows: list[str] = []
    for entry in trail["entries"]:
        # Attempt number for a step inside the retry loop, the acting reviewer for
        # a human step — the two things a flat log leaves the reader to infer.
        qualifier = ""
        if entry["attempt"] and numbered:
            qualifier = f"attempt {entry['attempt']}"
        elif entry["actor"]:
            role = f" ({entry['actor_role']})" if entry["actor_role"] else ""
            qualifier = f"{entry['actor']}{role}"
        if entry["revision"]:
            qualifier = f"revision {entry['revision']}" + (f" · {qualifier}" if qualifier else "")
        rows.append(
            f"<tr><td>{_esc(_timestamp(entry['timestamp']))}"
            f'<div class="detail">{_esc(entry["elapsed"])}</div></td>'
            f"<td>{_esc(entry['label'])}"
            f'<div class="detail">{_esc(qualifier)}</div></td>'
            f"<td>{_tag(entry['outcome'], _OUTCOME_TAGS.get(entry['status'], 'plain'))}</td>"
            f"<td>{_esc(entry['detail'])}</td></tr>"
        )
    table = _table(("When", "Step", "Outcome", "Detail"), rows)
    if not table:
        return ""
    return _section(
        "Execution log",
        _timeline_lead(trail["summary"]) + table,
        region="report-events",
    )


# --- Document ---------------------------------------------------------------


def has_reportable_content(payload: Mapping[str, Any]) -> bool:
    """Whether this run produced anything a report could be about.

    False only for a screening that was uploaded but never streamed: no
    checkpoint, so no criteria, no findings, no log — the same `neverRan` condition
    the run detail view renders its "never ran" notice for. Exporting that would
    hand a reviewer a branded page with a filename on it and nothing else, which
    reads as a broken feature rather than as an empty run.
    """
    values = _mapping(payload.get("values"))
    return bool(values.get("parsed_criteria")) or bool(_items(values, "events"))


def report_filename(payload: Mapping[str, Any]) -> str:
    """The download's filename: `trialgate-report-<protocol>-<run>.html`.

    Built from the sanitized protocol name plus the run id's first block, so a
    reviewer with a folder of these can tell them apart — and so two reports of
    the same protocol don't overwrite each other. Re-sanitized here rather than
    trusted: the stored name went through `sanitize_filename` on upload, and this
    value is interpolated into a `Content-Disposition` header where a quote or a
    newline would be a header-injection bug, not a cosmetic one.
    """
    record = _mapping(payload.get("screening"))
    values = _mapping(payload.get("values"))
    stem = sanitize_filename(
        str(record.get("source_filename") or values.get("source_filename") or "screening")
    ).rsplit(".", 1)[0]
    run = sanitize_filename(str(record.get("thread_id") or ""))[:8]
    parts = [part for part in ("trialgate-report", stem or "screening", run) if part]
    return f"{'-'.join(parts)}.html"


def render_report(payload: Mapping[str, Any], *, generated_at: datetime | None = None) -> str:
    """One run's report, as a complete HTML document.

    `payload` is exactly what `GET /api/screenings/{thread_id}/state` returns
    (`{values, pending, screening}`) — the report is rendered from the same payload
    the run detail view renders, so the document a reviewer hands off cannot
    disagree with the screen they exported it from.

    Sections drop out when the run has nothing for them (no findings, no cohort, no
    reviewer edits) rather than printing empty tables; `_phase_note` is what tells
    the reader *why* a section is missing when the run simply hasn't got there yet.
    """
    values = _mapping(payload.get("values"))
    stamped = generated_at or datetime.now(UTC)
    body = "".join(
        (
            _masthead(payload, stamped),
            _phase_note(payload),
            _criteria_section(values),
            _edits_section(values),
            _findings_section(values),
            _authorization_section(values),
            _cohort_section(values),
            _events_section(values),
        )
    )
    record = _mapping(payload.get("screening"))
    title = record.get("source_filename") or values.get("source_filename") or "screening"
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TrialGate screening report — {_esc(title)}</title>
<style>{_STYLES}</style>
</head>
<body>
{body}
<footer class="stamp">
<p>Generated by TrialGate on {_esc(_timestamp(stamped))}.</p>
<p>{_esc(DISCLAIMER)}</p>
</footer>
</body>
</html>
"""
