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

  useEffect(() => {
    if (!threadId) return;
    setNodeStates({});
    setPhase("running");

    const es = new EventSource(`/api/screenings/${threadId}/stream`);
    es.onmessage = (e) => {
      const msg: StreamMessage = JSON.parse(e.data);
      if (msg.node === "__interrupt__") {
        setPhase("awaiting_approval");
        es.close();
        return;
      }
      if (msg.node === "__end__") {
        setPhase("done");
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

  return { nodeStates, phase, setPhase };
}
