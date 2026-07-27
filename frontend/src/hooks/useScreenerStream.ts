"use client";

import { useCallback, useEffect, useState } from "react";
import type { StateUpdate, StreamMessage } from "@/types";

export type NodeState = { status: string; update: StateUpdate };
export type Phase = "idle" | "running" | "awaiting_approval" | "done" | "failed";

/**
 * Consumes the backend SSE streams. Each `stream_mode="updates"` event maps 1:1
 * onto a node execution, so we light up the agent card that just ran.
 *
 * Two producers share the same frame contract: the initial GET /stream
 * (EventSource, below) and the POST /approve stream that resumes the matcher
 * (consumed by the caller via fetch — EventSource can't POST). Both funnel frames
 * through `applyFrame`, so the wire handling lives in exactly one place.
 *
 * One screening per mount: `threadId` seeds the initial phase and is expected to
 * be fixed for the lifetime of the component. Callers start a new screening by
 * remounting on a `key` (see ScreeningRun's use in app/page.tsx) rather than
 * swapping the prop, which is what keeps the previous run's node states from
 * bleeding into the next one without a cascading state reset inside an effect.
 */
export function useScreenerStream(threadId: string | null) {
  const [nodeStates, setNodeStates] = useState<Record<string, NodeState>>({});
  const [phase, setPhase] = useState<Phase>(threadId ? "running" : "idle");
  const [error, setError] = useState<string | null>(null);

  // Apply one parsed frame to state. Returns true when the frame is terminal
  // (the caller should stop reading / close its source).
  const applyFrame = useCallback((msg: StreamMessage): boolean => {
    if (msg.node === "__interrupt__") {
      setPhase("awaiting_approval");
      return true;
    }
    if (msg.node === "__end__") {
      // A terminal node that set phase="failed" (e.g. human_escalation) arrives
      // just before __end__ — don't clobber it back to "done".
      setPhase((prev) => (prev === "failed" ? prev : "done"));
      return true;
    }
    if (msg.node === "__error__") {
      setError(msg.message ?? "Screening failed");
      setPhase("failed");
      return true;
    }
    if (msg.node === "__progress__") {
      // Non-terminal keepalive from a long node (the matcher). Show it as live
      // activity on the matcher card until its real terminal update arrives.
      const p = (msg.update ?? {}) as { done?: number; total?: number };
      const detail =
        p.total && p.total > 0
          ? `Matching cohort… (${Math.min((p.done ?? 0) + 1, p.total)}/${p.total})`
          : "Matching cohort…";
      setNodeStates((prev) => ({
        ...prev,
        matcher: {
          status: "running",
          update: { events: [{ agent: "matcher", status: "started", detail, timestamp: "" }] },
        },
      }));
      return false;
    }
    if (msg.node === "human_escalation") setPhase("failed");
    setNodeStates((prev) => ({
      ...prev,
      [msg.node]: { status: "completed", update: msg.update ?? {} },
    }));
    return false;
  }, []);

  useEffect(() => {
    if (!threadId) return;

    const es = new EventSource(`/api/screenings/${encodeURIComponent(threadId)}/stream`);
    es.onmessage = (e) => {
      const msg: StreamMessage = JSON.parse(e.data);
      if (applyFrame(msg)) es.close();
    };
    es.onerror = () => {
      es.close();
      // A terminal frame already closed the stream on the happy path, so reaching
      // here means the connection dropped mid-screening. EventSource exposes no
      // detail about why, but staying on "running" would leave the pipeline
      // spinning forever — say so instead. Guarded on the phase so a drop after
      // the gate or after completion doesn't rewrite a state the user has.
      setPhase((prev) => (prev === "running" ? "failed" : prev));
      // Keep any more specific error (an `__error__` frame) that already landed.
      setError(
        (prev) => prev ?? "Connection to the screening stream failed. Reload to check its status."
      );
    };
    return () => es.close();
  }, [threadId, applyFrame]);

  return { nodeStates, phase, setPhase, error, setError, applyFrame };
}
