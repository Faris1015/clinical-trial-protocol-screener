import type { NodeState } from "../hooks/useScreenerStream";

const LABELS: Record<string, string> = {
  router: "1 · Router",
  parser: "2 · Parser",
  critic: "3 · Regulatory Critic",
  matcher: "4 · Patient Matcher",
  human_escalation: "⚠ Human Escalation",
};

export function AgentCard({
  id,
  active,
  state,
}: {
  id: string;
  active: boolean;
  state?: NodeState;
}) {
  const events = state?.update.events ?? [];
  const latest = events[events.length - 1];
  return (
    <div className={`agent-card ${active ? "active" : ""} ${latest?.status ?? ""}`}>
      <h3>{LABELS[id] ?? id}</h3>
      <div className="status">{latest?.status ?? (state ? "completed" : "idle")}</div>
      {latest && <p className="detail">{latest.detail}</p>}
    </div>
  );
}
