"""Locust load test for the protocol screener — the real user journey (#10).

Each simulated user runs the full flow a reviewer does:

    1. POST /api/screenings            upload a protocol   -> thread_id
    2. GET  .../{id}/stream (SSE)       hold the live stream until the graph
                                        interrupts at the human-in-the-loop gate
    3. POST .../{id}/approve            approve past the gate -> matched patients
    4. GET  .../{id}/state             fetch the final results

Run the server with ``LLM_PROVIDER=stub`` (see docs/performance.md) so this
measures the app's own overhead — routing, SSE fan-out, the concurrency gate,
the checkpointer — not real model inference. ``STUB_LATENCY_SECONDS`` on the
server models a slow backend when you want to see how inference time interacts
with the threadpool and the concurrency gate.

Invoke via ``make loadtest`` (headless, 50 users) or directly:

    locust -f loadtest/locustfile.py --host http://localhost:8000

Backpressure is expected, not failure: when the concurrency gate is saturated
the server returns 429 + Retry-After. Those responses are marked successful (so
the failure rate reflects real defects only — 5xx, timeouts, malformed bodies),
and the user simply abandons that journey and starts a new one on its next task.
"""

from __future__ import annotations

import json
import os
import uuid

from locust import HttpUser, between, task

# A minimal protocol the Router admits (>= 200 chars, has an eligibility
# section, no pregnancy keywords that would trip the Critic's PREG-001 rule).
# Uploaded as markdown so the run isn't gated on PyMuPDF — PDF extraction is a
# separate, CPU-bound concern measured on its own (see docs/performance.md).
PROTOCOL_TEXT = (
    "# Phase II Single-Arm Study (load-test fixture)\n\n"
    "This is a synthetic protocol used only for load testing the screener.\n\n"
    "## Inclusion criteria\n"
    "- Age 18 years or older at the time of consent.\n"
    "- Adequate bone marrow and organ function per investigator assessment.\n\n"
    "## Exclusion criteria\n"
    "- Any concurrent condition that, in the investigator's opinion, would\n"
    "  compromise participant safety or study integrity.\n"
    "- Participation in another interventional trial within the prior 30 days.\n"
).encode("utf-8")

# How long a single user will hold the SSE stream waiting for the interrupt
# before giving up. Generous so a slow-backend run (STUB_LATENCY_SECONDS > 0)
# doesn't record spurious timeouts; tune down for a fast-path baseline.
STREAM_TIMEOUT_S = float(os.environ.get("LOADTEST_STREAM_TIMEOUT", "60"))

# Terminal SSE sentinels the frontend branches on (see app/services/sse.py).
_TERMINALS = {"__interrupt__", "__end__", "__error__"}


class ScreenerUser(HttpUser):
    """One reviewer running the upload -> stream -> approve -> results journey."""

    # Real reviewers pause between actions; a small think-time keeps the load
    # closed-loop rather than a synthetic hammer that only measures the client.
    wait_time = between(0.5, 2.0)

    @task
    def screen_protocol(self) -> None:
        thread_id = self._create_screening()
        if thread_id is None:
            return
        if not self._hold_stream_until_interrupt(thread_id):
            return
        self._approve(thread_id)
        self._fetch_results(thread_id)

    # --- journey steps -------------------------------------------------------

    def _create_screening(self) -> str | None:
        files = {
            "file": (f"protocol-{uuid.uuid4().hex}.md", PROTOCOL_TEXT, "text/markdown")
        }
        with self.client.post(
            "/api/screenings",
            files=files,
            name="POST /api/screenings",
            catch_response=True,
        ) as resp:
            if resp.status_code == 429:
                resp.success()  # rate/concurrency backpressure, not a defect
                return None
            if resp.status_code != 200:
                resp.failure(f"create failed: {resp.status_code}")
                return None
            resp.success()
            try:
                return str(resp.json()["thread_id"])
            except (ValueError, KeyError) as exc:
                resp.failure(f"create: bad body: {exc}")
                return None

    def _hold_stream_until_interrupt(self, thread_id: str) -> bool:
        """Open the SSE stream and read frames until the graph interrupts.

        Returns True once the human-in-the-loop gate is reached (the point a real
        UI enables the Approve button). A saturation 429 before the stream opens
        is expected backpressure and returns False without failing the request.
        """
        with self.client.get(
            f"/api/screenings/{thread_id}/stream",
            name="GET .../stream (SSE)",
            stream=True,
            catch_response=True,
            headers={"Accept": "text/event-stream"},
            # Hard cap on the read so a wedged stream fails the request instead
            # of pinning a worker forever; the server's own idle timeout is longer.
            timeout=STREAM_TIMEOUT_S,
        ) as resp:
            if resp.status_code == 429:
                resp.success()
                return False
            if resp.status_code != 200:
                resp.failure(f"stream failed: {resp.status_code}")
                return False
            try:
                node = self._read_until_terminal(resp)
            except Exception as exc:  # noqa: BLE001 — any stream error is a failed request
                resp.failure(f"stream read error: {exc}")
                return False
            if node == "__interrupt__":
                resp.success()
                return True
            if node is None:
                resp.failure(
                    f"stream produced no terminal frame within {STREAM_TIMEOUT_S}s"
                )
                return False
            # __end__ or __error__: a screening that never reached the gate. Not
            # a client fault, but not an approvable journey either.
            resp.success()
            return False

    def _read_until_terminal(self, resp: object) -> str | None:
        """Parse SSE lines until a terminal node sentinel; None on clean EOF.

        Heartbeats (comment lines starting ':') are skipped — they keep the
        socket warm and never carry a node. A wedged stream is bounded by the
        request's read timeout (raised as an exception, caught by the caller),
        so this loop needs no deadline of its own.
        """
        for raw in resp.iter_lines(decode_unicode=True):  # type: ignore[attr-defined]
            if raw is None:
                continue
            line = raw.strip()
            if not line or line.startswith(":"):
                continue  # blank separator or heartbeat comment
            if not line.startswith("data:"):
                continue
            payload = line[len("data:") :].strip()
            try:
                node = json.loads(payload).get("node")
            except ValueError:
                continue
            if node in _TERMINALS:
                return str(node)
        return None

    def _approve(self, thread_id: str) -> None:
        with self.client.post(
            f"/api/screenings/{thread_id}/approve",
            name="POST .../approve",
            catch_response=True,
        ) as resp:
            if resp.status_code == 429:
                resp.success()
                return
            if resp.status_code != 200:
                resp.failure(f"approve failed: {resp.status_code}")
                return
            resp.success()

    def _fetch_results(self, thread_id: str) -> None:
        with self.client.get(
            f"/api/screenings/{thread_id}/state",
            name="GET .../state",
            catch_response=True,
        ) as resp:
            if resp.status_code == 200:
                resp.success()
            else:
                resp.failure(f"state failed: {resp.status_code}")
