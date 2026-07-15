"""Graph assembly: nodes, conditional edges, the Critic loop, and the HITL gate.

`interrupt_before=["matcher"]` pauses the graph after the Critic approves —
a human reviews the parsed criteria before any patient data is touched. The
checkpointer persists every state transition per thread_id, making runs
resumable and inspectable.
"""

import time
from collections.abc import Callable
from typing import TypeVar, cast

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.graph.nodes.critic import critic_node, critic_router
from app.graph.nodes.matcher import matcher_node
from app.graph.nodes.parser import parser_node, parser_router
from app.graph.nodes.router import route_input, router_node
from app.graph.state import ScreenerState, event
from app.logging_config import get_logger

log = get_logger("graph")

NodeFn = TypeVar("NodeFn", bound=Callable[[ScreenerState], dict])


def _instrument(name: str, fn: NodeFn) -> NodeFn:
    """Wrap a node so every run logs its start, duration, and outcome.

    Server-side counterpart to the in-state event log: the `request_id` and
    `thread_id` bound by the API layer ride along via contextvars, so these
    lines join a single screening's story. Node bodies still log their own
    domain detail (retries, critic rejections) at the appropriate level.
    """

    def wrapped(state: ScreenerState) -> dict:
        # Resolved per-call rather than closing over a module-level logger, so
        # the render config in force at request time (LOG_FORMAT) always wins.
        node_log = get_logger("graph").bind(node=name)
        node_log.info("node.start")
        started = time.perf_counter()
        try:
            result = fn(state)
        except Exception:
            node_log.error(
                "node.error",
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
                exc_info=True,
            )
            raise
        node_log.info(
            "node.finish",
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
            outcome=result.get("current_step"),
        )
        return result

    return cast(NodeFn, wrapped)


def human_escalation_node(state: ScreenerState) -> dict:
    log.warning("critic.escalated", attempts=state["parse_attempts"])
    return {
        "current_step": "escalated",
        "events": [
            event(
                "critic",
                "escalated",
                f"Could not converge after {state['parse_attempts']} attempts — "
                "human review required",
            )
        ],
    }


def build_graph(checkpointer: BaseCheckpointSaver) -> CompiledStateGraph:
    g = StateGraph(ScreenerState)
    g.add_node("router", _instrument("router", router_node))
    g.add_node("parser", _instrument("parser", parser_node))
    g.add_node("critic", _instrument("critic", critic_node))
    g.add_node("matcher", _instrument("matcher", matcher_node))
    g.add_node("human_escalation", _instrument("human_escalation", human_escalation_node))

    g.add_edge(START, "router")
    g.add_conditional_edges("router", route_input, {"parser": "parser", "reject": END})
    # A parser that absorbed a failure (LLM down, unrepairable output) ends the
    # run cleanly instead of handing the Critic an empty extraction.
    g.add_conditional_edges("parser", parser_router, {"critic": "critic", "failed": END})
    g.add_conditional_edges(
        "critic",
        critic_router,
        {
            "matcher": "matcher",
            "parser": "parser",
            "human_escalation": "human_escalation",
        },
    )
    g.add_edge("matcher", END)
    g.add_edge("human_escalation", END)

    return g.compile(checkpointer=checkpointer, interrupt_before=["matcher"])
