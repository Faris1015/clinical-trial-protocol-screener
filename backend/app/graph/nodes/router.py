"""Agent 1: Router — validates input and decides whether to admit it to the pipeline."""

from app.graph.state import ScreenerState, event
from app.logging_config import get_logger

MIN_PROTOCOL_LENGTH = 200
ELIGIBILITY_MARKERS = ["inclusion", "exclusion", "eligib"]

log = get_logger("router")


def router_node(state: ScreenerState) -> dict:
    text = state["raw_protocol_text"]
    looks_like_protocol = len(text) >= MIN_PROTOCOL_LENGTH and any(
        marker in text.lower() for marker in ELIGIBILITY_MARKERS
    )
    if not looks_like_protocol:
        log.warning("router.rejected", text_chars=len(text))
        return {
            "current_step": "failed",
            "events": [
                event(
                    "router", "rejected", "Input does not appear to contain an eligibility section"
                )
            ],
        }
    return {
        "current_step": "parsing",
        "parse_attempts": 0,
        "events": [
            event(
                "router",
                "completed",
                f"Admitted '{state['source_filename']}' ({len(text)} chars of eligibility text)",
            )
        ],
    }


def route_input(state: ScreenerState) -> str:
    return "reject" if state["current_step"] == "failed" else "parser"
