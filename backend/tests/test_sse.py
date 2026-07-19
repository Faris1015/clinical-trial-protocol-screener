"""SSE wire-format helper (#3): frames are well-formed and the sentinel names
match what the frontend switches on."""

import asyncio
import json
from collections.abc import AsyncIterator

from app.services import sse


def test_frame_is_data_prefixed_and_double_newline_terminated():
    out = sse.frame({"node": "router"})
    assert out.startswith("data: ")
    assert out.endswith("\n\n")
    assert json.loads(out.removeprefix("data: ")) == {"node": "router"}


def test_update_frame_carries_node_and_update():
    payload = json.loads(
        sse.update_frame("parser", {"current_step": "parsing"}).removeprefix("data: ")
    )
    assert payload == {"node": "parser", "update": {"current_step": "parsing"}}


def test_terminal_frames_use_the_sentinel_node_names():
    assert json.loads(sse.interrupt_frame().removeprefix("data: ")) == {"node": "__interrupt__"}
    assert json.loads(sse.end_frame().removeprefix("data: ")) == {"node": "__end__"}
    assert json.loads(sse.error_frame("boom").removeprefix("data: ")) == {
        "node": "__error__",
        "message": "boom",
    }


# --- heartbeats + idle timeout (#15) ---------------------------------------


def test_heartbeat_is_an_sse_comment_ignored_by_eventsource():
    # Leading colon = comment; the browser's EventSource never fires onmessage.
    assert sse.HEARTBEAT.startswith(":")
    assert sse.HEARTBEAT.endswith("\n\n")
    assert not sse.HEARTBEAT.startswith("data:")


async def test_with_heartbeats_passes_frames_through_without_delay():
    async def frames() -> AsyncIterator[str]:
        yield sse.update_frame("router", {})
        yield sse.end_frame()

    out = [
        f
        async for f in sse.with_heartbeats(frames(), heartbeat_seconds=10, idle_timeout_seconds=100)
    ]
    assert out == [sse.update_frame("router", {}), sse.end_frame()]


async def test_with_heartbeats_injects_heartbeat_on_silence():
    async def frames() -> AsyncIterator[str]:
        await asyncio.sleep(0.05)  # silent longer than the heartbeat window
        yield sse.end_frame()

    out = [
        f
        async for f in sse.with_heartbeats(
            frames(), heartbeat_seconds=0.01, idle_timeout_seconds=100
        )
    ]
    assert sse.HEARTBEAT in out
    assert out[-1] == sse.end_frame()


async def test_with_heartbeats_reaps_idle_stream_with_error_frame():
    async def never_yields() -> AsyncIterator[str]:
        await asyncio.sleep(10)
        yield sse.end_frame()  # pragma: no cover - never reached

    out = [
        f
        async for f in sse.with_heartbeats(
            never_yields(), heartbeat_seconds=0.01, idle_timeout_seconds=0.05
        )
    ]
    # Terminates on its own with a terminal __error__ frame — no infinite hang.
    last = json.loads(out[-1].removeprefix("data: "))
    assert last["node"] == sse.ERROR
    assert "idle timeout" in last["message"].lower()


async def test_with_heartbeats_real_frames_reset_idle_clock():
    # A steady drip of real frames (each just under the idle window) must keep
    # the stream alive well past idle_timeout — the clock resets on data.
    async def drip() -> AsyncIterator[str]:
        for i in range(5):
            await asyncio.sleep(0.02)
            yield sse.update_frame(f"n{i}", {})

    out = [
        f
        async for f in sse.with_heartbeats(
            drip(), heartbeat_seconds=0.01, idle_timeout_seconds=0.05
        )
    ]
    data_frames = [f for f in out if f.startswith("data:")]
    # All 5 real frames survive; none is a terminal idle-timeout error.
    assert len(data_frames) == 5
    assert all("__error__" not in f for f in data_frames)
