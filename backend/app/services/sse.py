"""Serialize screening stream events into the Server-Sent Events wire format —
the single place that knows the SSE frame layout and its sentinel node names.

Every frame the browser receives is a `data:` line built here; the frontend's
`useScreenerStream` hook switches on the sentinel node names below, so the wire
contract lives in exactly one module on each side.
"""

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator

# Sentinel "node" values the frontend branches on to end or interrupt its live
# execution view (see frontend/src/hooks/useScreenerStream.ts).
INTERRUPT = "__interrupt__"
ERROR = "__error__"
END = "__end__"

# A heartbeat is an SSE *comment* line (leading colon). Browsers' EventSource
# ignores it — it never fires `onmessage` — so it keeps the connection warm and
# surfaces a dead socket (the write fails) without the frontend having to parse
# it. See MDN "Using server-sent events".
HEARTBEAT = ": heartbeat\n\n"


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


async def with_heartbeats(
    frames: AsyncIterator[str], *, heartbeat_seconds: float, idle_timeout_seconds: float
) -> AsyncIterator[str]:
    """Interleave heartbeat comments into a frame stream and reap a dead stream.

    Emits a `: heartbeat` comment after each `heartbeat_seconds` of silence
    (keeping proxies from cutting an idle connection and letting a write to a
    departed client fail fast). If nothing real arrives for `idle_timeout_seconds`
    the stream is terminated with a final `__error__` frame — a wedged graph or a
    client that vanished without a socket error no longer leaks the generator.

    Real frames reset the idle clock; heartbeats do not, so a genuinely stalled
    run still hits the timeout even while heartbeats keep firing.
    """
    iterator = frames.__aiter__()
    idle = 0.0
    # Wait on a *persistent* task, never `wait_for(__anext__())`: a timed-out
    # `wait_for` cancels its awaitable, and cancelling `__anext__` injects
    # CancelledError into the underlying generator, killing the real stream. We
    # instead let the pending pull keep running across heartbeat ticks.
    pending = asyncio.ensure_future(iterator.__anext__())
    try:
        while True:
            done, _ = await asyncio.wait({pending}, timeout=heartbeat_seconds)
            if not done:
                idle += heartbeat_seconds
                if idle >= idle_timeout_seconds:
                    yield error_frame("Screening stream idle timeout — connection closed.")
                    return
                yield HEARTBEAT
                continue
            try:
                frame_str = pending.result()
            except StopAsyncIteration:
                return
            idle = 0.0
            yield frame_str
            pending = asyncio.ensure_future(iterator.__anext__())
    finally:
        pending.cancel()
        with contextlib.suppress(BaseException):
            await pending
        # Close the source generator so its own `finally` (e.g. the concurrency
        # slot release) runs even when we stop pulling early.
        aclose = getattr(iterator, "aclose", None)
        if aclose is not None:
            with contextlib.suppress(BaseException):
                await aclose()
