"""Agent 2: Parser — LLM extraction into the typed CriteriaSchema.

On a Critic loop-back, the prompt includes the previous extraction plus the
Critic's structured objections, so retries converge instead of re-rolling.

Failure containment: transient LLM errors are retried inside
invoke_with_retry; a schema-validation failure gets exactly one repair
attempt (the validation error is fed back to the model); anything still
failing lands the screening in a terminal `failed` state with a user-visible
event instead of an unhandled exception killing the stream.
"""

from langchain_core.exceptions import OutputParserException
from langchain_core.runnables import Runnable
from pydantic import ValidationError

from app.exceptions import LLMUnavailableError
from app.graph.state import ScreenerState, event
from app.logging_config import get_logger
from app.schemas.criteria import CriteriaSchema
from app.services.llm import get_llm, invoke_with_retry

log = get_logger("parser")

PARSER_SYSTEM = """You are a clinical protocol extraction engine. Extract eligibility \
criteria into the exact schema provided. Rules:
1. Every numeric threshold MUST become a QuantitativeCriterion with operator and unit.
2. Never invent values. If a criterion is vague (e.g. "adequate organ function" with no \
numbers), put its verbatim text in `unparseable`.
3. `source_text` must be copied verbatim from the protocol.
4. Exclusion criteria describing a required ABSENCE go in exclusion lists, not inclusion \
with negated=true."""


def _validate(raw: object) -> CriteriaSchema:
    # with_structured_output may return the model instance or a plain dict
    return raw if isinstance(raw, CriteriaSchema) else CriteriaSchema.model_validate(raw)


def _extract_criteria(structured_llm: Runnable, prompt: str) -> CriteriaSchema:
    """One extraction, with a single repair round-trip on schema violations."""
    messages: list[tuple[str, str]] = [("system", PARSER_SYSTEM), ("user", prompt)]
    try:
        return _validate(invoke_with_retry(structured_llm, messages))
    except (ValidationError, OutputParserException) as exc:
        log.warning("parser.schema_repair", error=type(exc).__name__)
        repair = [
            *messages,
            (
                "user",
                "Your previous response failed schema validation:\n"
                f"{exc}\n"
                "Return a corrected extraction that strictly matches the schema.",
            ),
        ]
        return _validate(invoke_with_retry(structured_llm, repair))


def _failed(detail: str) -> dict:
    return {
        "current_step": "failed",
        "events": [event("parser", "failed", detail)],
    }


def parser_node(state: ScreenerState) -> dict:
    structured_llm = get_llm().with_structured_output(CriteriaSchema)
    attempt = state.get("parse_attempts", 0) + 1
    is_revision = bool(state.get("critic_feedback"))
    log.info("parser.extracting", attempt=attempt, revision=is_revision)

    prompt = state["raw_protocol_text"]
    if state.get("critic_feedback"):
        prompt += (
            "\n\n--- REVISION REQUIRED ---\n"
            "A compliance reviewer rejected your previous extraction:\n"
            f"{state['critic_feedback']}\n"
            f"Your previous extraction was:\n{state['parsed_criteria']}\n"
            "Produce a corrected extraction addressing every point."
        )

    try:
        result = _extract_criteria(structured_llm, prompt)
    except LLMUnavailableError as exc:
        log.error("parser.llm_unavailable", attempt=attempt, detail=str(exc))
        return _failed(str(exc))
    except (ValidationError, OutputParserException):
        log.error("parser.schema_validation_exhausted", attempt=attempt)
        return _failed(
            "Model output failed schema validation twice (original + repair attempt) — "
            "screening aborted."
        )

    n_inc = len(result.inclusion_quantitative) + len(result.inclusion_categorical)
    n_exc = len(result.exclusion_quantitative) + len(result.exclusion_categorical)
    log.info(
        "parser.extracted",
        attempt=attempt,
        inclusion=n_inc,
        exclusion=n_exc,
        unparseable=len(result.unparseable),
    )
    return {
        "parsed_criteria": result.model_dump(),
        "parse_attempts": state.get("parse_attempts", 0) + 1,
        "current_step": "critiquing",
        "events": [
            event(
                "parser",
                "completed",
                f"Extracted {n_inc} inclusion / {n_exc} exclusion criteria, "
                f"{len(result.unparseable)} unparseable "
                f"(attempt {state.get('parse_attempts', 0) + 1})",
            )
        ],
    }


def parser_router(state: ScreenerState) -> str:
    """A failed extraction ends the run; a successful one goes to the Critic."""
    return "failed" if state["current_step"] == "failed" else "critic"
