"""Graph assembly: nodes, conditional edges, the Critic loop, and the HITL gate.

`interrupt_before=["matcher"]` pauses the graph after the Critic approves —
a human reviews the parsed criteria before any patient data is touched. The
checkpointer persists every state transition per thread_id, making runs
resumable and inspectable.
"""

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.graph.nodes.critic import critic_node, critic_router
from app.graph.nodes.matcher import matcher_node
from app.graph.nodes.parser import parser_node, parser_router
from app.graph.nodes.router import route_input, router_node
from app.graph.state import ScreenerState, event


def human_escalation_node(state: ScreenerState) -> dict:
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


def build_graph() -> CompiledStateGraph:
    g = StateGraph(ScreenerState)
    g.add_node("router", router_node)
    g.add_node("parser", parser_node)
    g.add_node("critic", critic_node)
    g.add_node("matcher", matcher_node)
    g.add_node("human_escalation", human_escalation_node)

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

    return g.compile(checkpointer=MemorySaver(), interrupt_before=["matcher"])


graph = build_graph()
