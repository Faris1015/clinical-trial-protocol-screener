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


class ScreeningNotApprovableError(ScreenerError):
    """Approval was requested for a screening that isn't parked at the gate."""

    http_status = 409


class ScreeningNotEditableError(ScreenerError):
    """Criteria edits were submitted for a run that isn't at a reviewable stop (#53).

    Editable means parked at the approval gate, escalated, or failed *with an
    extraction to correct*. A finished run is not: its cohort was already scored
    against the criteria it had, so re-running it under different criteria would
    quietly rewrite history rather than produce a new, attributable run.
    """

    http_status = 409


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
