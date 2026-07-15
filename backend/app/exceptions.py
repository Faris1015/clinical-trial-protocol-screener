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
