import { useState } from "react";
import { useScreenerStream } from "./hooks/useScreenerStream";
import { AgentCard } from "./components/AgentCard";
import { CriteriaTable } from "./components/CriteriaTable";
import { PatientMatchTable } from "./components/PatientMatchTable";
import type { ApproveResponse, PatientEvaluation } from "./types";
import "./styles.css";

const AGENTS = ["router", "parser", "critic", "matcher"];

export default function App() {
  const [threadId, setThreadId] = useState<string | null>(null);
  const [matches, setMatches] = useState<PatientEvaluation[]>([]);
  const { nodeStates, phase, setPhase } = useScreenerStream(threadId);

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
    const res = await fetch(`/api/screenings/${threadId}/approve`, { method: "POST" });
    const data = (await res.json()) as ApproveResponse;
    setMatches(data.matched_patients);
    setPhase("done");
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
          Could not converge — escalated to human review after 3 attempts.
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
