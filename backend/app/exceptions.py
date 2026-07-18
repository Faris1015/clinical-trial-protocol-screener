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


class PayloadTooLargeError(ScreenerError):
    """An upload exceeded the configured size cap."""

    http_status = 413


class UnsupportedMediaTypeError(ScreenerError):
    """An upload's content type is not in the allowlist."""

    http_status = 415


class TooManyActiveScreeningsError(ScreenerError):
    """Every concurrency slot is in use; the caller should retry shortly."""

    http_status = 429
