import { useState } from "react";
import { useScreenerStream } from "./hooks/useScreenerStream";
import { AgentCard } from "./components/AgentCard";
import { CriteriaTable } from "./components/CriteriaTable";
import { PatientMatchTable } from "./components/PatientMatchTable";
import type { PatientEvaluation, StreamMessage } from "./types";
import "./styles.css";

const AGENTS = ["router", "parser", "critic", "matcher"];

export default function App() {
  const [threadId, setThreadId] = useState<string | null>(null);
  const [matches, setMatches] = useState<PatientEvaluation[]>([]);
  const { nodeStates, phase, setPhase, error, setError, applyFrame } = useScreenerStream(threadId);

  async function upload(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    const body = new FormData();
    body.append("file", file);
    const res = await fetch("/api/screenings", { method: "POST", body });
    const { thread_id } = (await res.json()) as { thread_id: string };
    setMatches([]);
    setThreadId(thread_id);
  }

  async function approve() {
    // Flip to "running" first: it hides the approval banner (and its button),
    // so a slow matcher can't be double-approved into a duplicate run.
    setError(null);
    setPhase("running");
    const res = await fetch(`/api/screenings/${encodeURIComponent(threadId!)}/approve`, {
      method: "POST",
    });
    if (!res.ok || !res.body) {
      // Eager-validation errors (404 unknown thread, 409 not at the gate, 429
      // slots full) arrive as JSON before the stream commits — the screening
      // stays parked at the gate, so show the error instead of hanging.
      const body = (await res.json().catch(() => ({}))) as { detail?: string };
      setError(body.detail ?? "Approval failed");
      setPhase("failed");
      return;
    }
    // The matcher streams over SSE like the initial phase; EventSource can't
    // POST, so read the body and split on the SSE frame delimiter ourselves,
    // funneling each frame through the shared reducer.
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let sep: number;
      while ((sep = buffer.indexOf("\n\n")) !== -1) {
        const block = buffer.slice(0, sep);
        buffer = buffer.slice(sep + 2);
        // Skip heartbeat comment lines (": heartbeat"); keep only data frames.
        const dataLine = block.split("\n").find((l) => l.startsWith("data:"));
        if (!dataLine) continue;
        const msg = JSON.parse(dataLine.slice("data:".length).trim()) as StreamMessage;
        if (msg.node === "matcher" && msg.update?.matched_patients) {
          setMatches(msg.update.matched_patients);
        }
        applyFrame(msg);
      }
    }
  }

  // Latest parsed criteria streamed from the parser node
  const parsed = nodeStates.parser?.update.parsed_criteria ?? null;
  const activeAgent =
    phase === "running" ? ([...AGENTS].reverse().find((a) => nodeStates[a]) ?? null) : null;

  return (
    <div className="app">
      <header>
        <h1>Clinical Trial Protocol Screener</h1>
        <p>Multi-agent · LangGraph · deterministic validation · human-in-the-loop</p>
      </header>

      <label className="upload">
        Upload protocol (PDF or .md)
        <input type="file" accept=".pdf,.md,.txt" onChange={upload} />
      </label>

      <section className="pipeline">
        {AGENTS.map((id) => (
          <AgentCard key={id} id={id} active={id === activeAgent} state={nodeStates[id]} />
        ))}
      </section>

      {phase === "failed" && (
        <div className="banner failed">
          {error ?? "Could not converge — escalated to human review after 3 attempts."}
        </div>
      )}

      {parsed && <CriteriaTable criteria={parsed} />}

      {phase === "awaiting_approval" && (
        <div className="banner approval">
          <span>
            Compliance checks passed. Review the criteria above, then approve patient matching.
          </span>
          <button onClick={approve}>Approve → run matching</button>
        </div>
      )}

      {matches.length > 0 && <PatientMatchTable patients={matches} />}
    </div>
  );
}
