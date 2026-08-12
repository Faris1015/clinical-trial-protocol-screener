"""Domain exception hierarchy.

Every failure mode the app knows how to talk about has a type here. Each
class carries the HTTP status it maps to, so the FastAPI handler in
app/main.py stays a one-liner and a new subclass can never be forgotten in a
status table — clients get a JSON body, never a raw stack trace. Graph nodes
catch precisely the failures they know how to absorb.
"""


class ScreenerError(Exception):
    """Base class for all domain errors."""

    http_status = 500

    def __init__(self, *args: object, headers: dict[str, str] | None = None) -> None:
        super().__init__(*args)
        # Extra response headers the error handler should emit (e.g. Retry-After
        # on a 429). Empty for the common case; the handler passes it through.
        self.headers: dict[str, str] = headers or {}


class LLMUnavailableError(ScreenerError):
    """The LLM backend could not be reached after exhausting retries."""

    http_status = 503


class ExtractionError(ScreenerError):
    """An uploaded document could not be read or parsed into text."""

    http_status = 422


class DataStoreError(ScreenerError):
    """A required data file (patient records, rules) is missing or corrupt."""

    http_status = 503


class ScreeningNotFoundError(ScreenerError):
    """No screening exists for the requested thread_id."""

    http_status = 404


class PatientNotFoundError(ScreenerError):
    """No patient in the synthetic cohort carries the requested id (#96).

    A 404 rather than an empty record: the reverse-matching page is reached by a
    link out of a run's cohort table, and a patient id that no longer resolves
    means the EHR was regenerated under a run that had already scored it — which
    a reader needs told, not shown as a patient with no history.
    """

    http_status = 404


class ScreeningNotApprovableError(ScreenerError):
    """Approval was requested for a screening that isn't parked at the gate."""

    http_status = 409


class ScreeningNotRejectableError(ScreenerError):
    """A rejection was submitted for a run that isn't at a decision point (#91).

    Rejectable means parked at the approval gate or escalated after the Critic
    gave up — the two states where a human owns the run and the only two from
    which stopping it is a decision rather than a rewrite of history. A finished
    run already produced a cohort, and a failed one already stopped; calling
    either "rejected" would overwrite what actually happened.
    """

    http_status = 409


class ScreeningNotEditableError(ScreenerError):
    """Criteria edits were submitted for a run that isn't at a reviewable stop (#53).

    Editable means parked at the approval gate, escalated, failed or finished —
    *with an extraction to correct*. A finished run was excluded until #95, when
    promoting a what-if simulation made re-running a scored run the point rather
    than an accident; it is attributable rather than a silent rewrite because the
    edit carries a revision, a diff and an editor, and the cohort it invalidates is
    discarded rather than left standing under new criteria.

    Still refused for a run that never parsed anything, and for one a reviewer
    rejected (#91) — editing that would erase a decision rather than reverse it.
    """

    http_status = 409


class ScreeningNotSimulatableError(ScreenerError):
    """A what-if was requested for a run with no cohort to re-score (#95).

    Simulation moves a threshold across the verdicts a run already produced, so it
    needs both an extraction and a scored cohort. A run still at the gate has the
    first and not the second; answering it with an all-zero table would read as
    "relaxing this changes nothing" rather than "this has not been screened yet".
    """

    http_status = 409


class InvalidSimulationError(ScreenerError):
    """A what-if named a criterion this run cannot simulate (#95).

    An unknown key, one named twice, or a categorical criterion — whose relaxation
    would mean re-matching a term against every patient record, an LLM pass rather
    than a what-if. Refused rather than skipped: a simulation that silently dropped
    an override would present the unchanged cohort as the simulated one.
    """

    http_status = 422


class CriteriaRevisionConflictError(ScreenerError):
    """Edits were based on a revision of the criteria that has since been replaced (#53).

    Two reviewers can open the same parked run. Without this check the second
    save would silently discard the first's corrections; with it, the loser is
    told to reload and re-apply.
    """

    http_status = 409


class ScreeningNotReportableError(ScreenerError):
    """A report was requested for a screening that never ran (#56).

    The upload exists but nothing was ever streamed for it, so there is no
    checkpoint: no criteria, no findings, no execution log. Exporting that would
    produce a branded page carrying only a filename, which reads as a broken
    feature rather than as an empty run. Every other phase — parked at the gate,
    escalated, failed partway, finished — has something to report and is allowed.
    """

    http_status = 409


class ScreeningNotExportableError(ScreenerError):
    """A cohort export was requested for a screening that never ran (#102).

    Deliberately the same condition — and the same status — as
    `ScreeningNotReportableError`: the two downloads sit beside each other in the
    UI and are two renderings of one snapshot, so a run one of them refuses and the
    other serves would be a defect a reader cannot resolve. A run that parsed but
    never matched is *not* this: its export is the approved criteria over an empty
    cohort, which is a true statement about the run.
    """

    http_status = 409


class PayloadTooLargeError(ScreenerError):
    """An upload exceeded the configured size cap."""

    http_status = 413


class UnsupportedMediaTypeError(ScreenerError):
    """An upload's content type is not in the allowlist."""

    http_status = 415


class InvalidBatchError(ScreenerError):
    """A batch upload carried more protocols than one submission may (#61).

    Distinct from a per-file rejection: an unreadable or disallowed document is
    reported as that item's own error and the rest of the batch still screens
    (see services/screening.create_screening_batch). This is the whole submission
    being refused before any of it is processed.
    """

    http_status = 422


class InvalidComparisonError(ScreenerError):
    """A side-by-side comparison was asked for between a run and itself (#59).

    Not answered with an all-identical table: the request is a mistyped link or a
    double-selected row, and a page solemnly confirming that a run matches itself
    would read as a working comparison of two runs.
    """

    http_status = 422


class InvalidAuditFilterError(ScreenerError):
    """The audit index was asked for a narrowing it cannot honor (#98).

    An unknown action, an unreadable date, or a range whose start is after its
    end. Refused rather than dropped: silently ignoring a filter returns a page
    *wider* than the one requested, and an auditor reading it would take an
    unscoped answer for a scoped one.
    """

    http_status = 422


class AuditExportTooLargeError(ScreenerError):
    """An audit export was asked for more decisions than one file may carry (#98).

    Refused rather than truncated. A CSV has nowhere to state that it holds only
    the newest N rows — a trailing note row would corrupt the parse the file
    exists to feed — so a silently short export would leave an auditor unable to
    tell a complete answer from a partial one. Narrowing the date range is the
    way through, and the message says the count so the caller knows by how much.

    413 rather than 422: the filter is well-formed, the *response* is what would
    be too large.
    """

    http_status = 413


class InvalidRuleError(ScreenerError):
    """An authored compliance rule does not satisfy its check kind's contract (#97).

    A missing id or description, an unknown check kind, a `range` without two
    numeric bounds or with them inverted, a `must_be_quantitative` with no
    keywords to bring it into scope — every way a rule can be written that the
    engine could not run, or could run only to no effect.

    The whole point of validating on write: `run_deterministic_checks` indexes
    into a rule with `rule["min_plausible"]`, so a malformed row is a KeyError in
    the middle of somebody's screening. Refusing it at authoring time turns that
    into a 422 for the admin who typed it, while they are looking at the form.
    """

    http_status = 422


class RuleNotFoundError(ScreenerError):
    """An edit or a retirement named a rule id that is not in the table (#97)."""

    http_status = 404


class DuplicateRuleError(ScreenerError):
    """A new rule reused an id the table already holds (#97).

    Refused rather than merged into an update: a finding cites a rule id forever,
    so silently rewriting the rule behind an existing id would change what every
    past finding means. Editing the existing rule is the deliberate way to do that,
    and it is a different request.
    """

    http_status = 409


class TooManyActiveScreeningsError(ScreenerError):
    """Every concurrency slot is in use; the caller should retry shortly."""

    http_status = 429


class AuthenticationRequiredError(ScreenerError):
    """The request carried no valid session (#50)."""

    http_status = 401


class InvalidCredentialsError(ScreenerError):
    """Login was attempted with an unknown email or a wrong password (#50).

    Deliberately indistinguishable between the two cases — the message must not
    tell an attacker which half they got right.
    """

    http_status = 401


class AuthorizationDeniedError(ScreenerError):
    """The caller is authenticated but their role doesn't cover this action (#50)."""

    http_status = 403
