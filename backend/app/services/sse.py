"""Serialize screening stream events into the Server-Sent Events wire format —
the single place that knows the SSE frame layout and its sentinel node names.

Every frame the browser receives is a `data:` line built here; the frontend's
`useScreenerStream` hook switches on the sentinel node names below, so the wire
contract lives in exactly one module on each side.
"""

import json

# Sentinel "node" values the frontend branches on to end or interrupt its live
# execution view (see frontend/src/hooks/useScreenerStream.ts).
INTERRUPT = "__interrupt__"
ERROR = "__error__"
END = "__end__"


def frame(payload: dict) -> str:
    """Render one SSE `data:` frame (double-newline terminated)."""
    return f"data: {json.dumps(payload)}\n\n"


def update_frame(node: str, update: dict) -> str:
    """A graph node's state update. `update` must already be JSON-serializable."""
    return frame({"node": node, "update": update})


def interrupt_frame() -> str:
    """The graph paused at the human-in-the-loop gate."""
    return frame({"node": INTERRUPT})


def error_frame(message: str) -> str:
    """A terminal failure — the frontend surfaces `message` to the reviewer."""
    return frame({"node": ERROR, "message": message})


def end_frame() -> str:
    """The run finished successfully."""
    return frame({"node": END})
