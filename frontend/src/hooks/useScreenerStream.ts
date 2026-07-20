import { useEffect, useState } from "react";
import type { StateUpdate, StreamMessage } from "../types";

export type NodeState = { status: string; update: StateUpdate };
export type Phase = "idle" | "running" | "awaiting_approval" | "done" | "failed";

/**
 * Consumes the backend SSE stream. Each `stream_mode="updates"` event maps 1:1
 * onto a node execution, so we light up the agent card that just ran.
 */
export function useScreenerStream(threadId: string | null) {
  const [nodeStates, setNodeStates] = useState<Record<string, NodeState>>({});
  const [phase, setPhase] = useState<Phase>("idle");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!threadId) return;
    setNodeStates({});
    setError(null);
    setPhase("running");

    const es = new EventSource(`/api/screenings/${encodeURIComponent(threadId)}/stream`);
    es.onmessage = (e) => {
      const msg: StreamMessage = JSON.parse(e.data);
      if (msg.node === "__interrupt__") {
        setPhase("awaiting_approval");
        es.close();
        return;
      }
      if (msg.node === "__end__") {
        // A terminal node that set phase="failed" (e.g. human_escalation)
        // arrives just before __end__ — don't clobber it back to "done".
        setPhase((prev) => (prev === "failed" ? prev : "done"));
        es.close();
        return;
      }
      if (msg.node === "__error__") {
        setError(msg.message ?? "Screening failed");
        setPhase("failed");
        es.close();
        return;
      }
      if (msg.node === "human_escalation") setPhase("failed");
      setNodeStates((prev) => ({
        ...prev,
        [msg.node]: { status: "completed", update: msg.update ?? {} },
      }));
    };
    es.onerror = () => es.close();
    return () => es.close();
  }, [threadId]);

  return { nodeStates, phase, setPhase, error, setError };
}
