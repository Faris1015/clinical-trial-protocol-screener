"""SSE wire-format helper (#3): frames are well-formed and the sentinel names
match what the frontend switches on."""

import json

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
