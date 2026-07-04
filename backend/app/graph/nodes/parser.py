"""Agent 2: Parser — LLM extraction into the typed CriteriaSchema.

On a Critic loop-back, the prompt includes the previous extraction plus the
Critic's structured objections, so retries converge instead of re-rolling.
"""
from app.graph.state import ScreenerState, event
from app.schemas.criteria import CriteriaSchema
from app.services.llm import get_llm

PARSER_SYSTEM = """You are a clinical protocol extraction engine. Extract eligibility \
criteria into the exact schema provided. Rules:
1. Every numeric threshold MUST become a QuantitativeCriterion with operator and unit.
2. Never invent values. If a criterion is vague (e.g. "adequate organ function" with no \
numbers), put its verbatim text in `unparseable`.
3. `source_text` must be copied verbatim from the protocol.
4. Exclusion criteria describing a required ABSENCE go in exclusion lists, not inclusion \
with negated=true."""


def parser_node(state: ScreenerState) -> dict:
    structured_llm = get_llm().with_structured_output(CriteriaSchema)

    prompt = state["raw_protocol_text"]
    if state.get("critic_feedback"):
        prompt += (
            "\n\n--- REVISION REQUIRED ---\n"
            "A compliance reviewer rejected your previous extraction:\n"
            f"{state['critic_feedback']}\n"
            f"Your previous extraction was:\n{state['parsed_criteria']}\n"
            "Produce a corrected extraction addressing every point."
        )

    result: CriteriaSchema = structured_llm.invoke(
        [("system", PARSER_SYSTEM), ("user", prompt)]
    )
    n_inc = len(result.inclusion_quantitative) + len(result.inclusion_categorical)
    n_exc = len(result.exclusion_quantitative) + len(result.exclusion_categorical)
    return {
        "parsed_criteria": result.model_dump(),
        "parse_attempts": state.get("parse_attempts", 0) + 1,
        "current_step": "critiquing",
        "events": [event("parser", "completed",
                         f"Extracted {n_inc} inclusion / {n_exc} exclusion criteria, "
                         f"{len(result.unparseable)} unparseable "
                         f"(attempt {state.get('parse_attempts', 0) + 1})")],
    }
