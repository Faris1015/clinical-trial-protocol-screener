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
from app.schemas.criteria import CategoricalCriterion, CriteriaSchema
from app.services.llm import get_llm, invoke_with_retry

log = get_logger("parser")

PARSER_SYSTEM = """You are a clinical protocol extraction engine. Extract eligibility \
criteria into the exact schema provided. Rules:
1. Every numeric threshold MUST become a QuantitativeCriterion with operator and unit. \
Never invent a number — if a criterion has no number, it is NOT quantitative (do not emit \
value=0 or a placeholder).
2. A criterion naming a diagnosis, biomarker, prior treatment, or condition (e.g. \
"Diagnosis of type 2 diabetes mellitus", "EGFR exon 19 deletion") is a CategoricalCriterion \
with the matching category — even though it has no number. `unparseable` is ONLY for \
criteria that imply a numeric threshold but state none (e.g. "adequate organ function"); \
never put a diagnosis or a plainly-checkable term there.
3. `source_text` must be copied verbatim from the protocol. Extract ONLY from the protocol \
text; never copy a reviewer's revision feedback into any field.
4. Exclusion criteria describing a required ABSENCE go in exclusion lists, not inclusion \
with negated=true.
5. Extract lab thresholds as the plain clinical number, never the scientific-notation \
expansion. A blood count written "N x 10^9/L" (platelets, ANC) has value N with unit \
"10^9/L" — e.g. "100 x 10^9/L" is value=100 (NOT 100000000000), "1.5 x 10^9/L" is \
value=1.5. Do not multiply the coefficient out."""


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


def _dedupe_categoricals(criteria: CriteriaSchema) -> CriteriaSchema:
    """Clean up redundant categorical extractions before they reach the Matcher.

    A weak model sometimes emits the same term in both an inclusion and an
    exclusion list (e.g. a GLP-1 agonist as a negated inclusion AND an
    exclusion — the same "must not have" meaning twice), repeats a term, or
    emits an empty-value criterion. Drop the cross-list duplicate from the
    inclusion side (the exclusion list is the term's natural home), collapse
    intra-list repeats, and discard empty values.
    """

    def _key(c: CategoricalCriterion) -> str:
        return c.value.strip().lower()

    def _clean(items: list[CategoricalCriterion], drop: set[str]) -> list[CategoricalCriterion]:
        seen: set[str] = set()
        out: list[CategoricalCriterion] = []
        for c in items:
            k = _key(c)
            if not k or k in drop or k in seen:
                continue
            seen.add(k)
            out.append(c)
        return out

    exclusion_keys = {_key(c) for c in criteria.exclusion_categorical if _key(c)}
    criteria.inclusion_categorical = _clean(criteria.inclusion_categorical, exclusion_keys)
    criteria.exclusion_categorical = _clean(criteria.exclusion_categorical, set())
    return criteria


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
            "Produce a corrected extraction addressing every point. Re-extract from the "
            "protocol text above; the feedback is guidance only — never copy its wording "
            "into source_text, unparseable, or any other field."
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

    result = _dedupe_categoricals(result)
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
