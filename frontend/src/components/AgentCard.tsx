import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import type { NodeState } from "@/hooks/useScreenerStream";
import type { AgentEvent } from "@/types";

const LABELS: Record<string, string> = {
  router: "1 · Router",
  parser: "2 · Parser",
  critic: "3 · Regulatory Critic",
  matcher: "4 · Patient Matcher",
  human_escalation: "⚠ Human Escalation",
};

/**
 * Which badge variant an agent's latest event reads as. `rejected`/`escalated`
 * are the critic pushing work back, which is a warning rather than a failure —
 * the graph retries. Only `failed` is terminal-bad.
 */
function statusVariant(status: AgentEvent["status"] | undefined) {
  switch (status) {
    case "completed":
      return "pass" as const;
    case "failed":
      return "fail" as const;
    case "rejected":
    case "escalated":
      return "warn" as const;
    default:
      return "secondary" as const;
  }
}

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
  const status = latest?.status ?? (state ? "completed" : undefined);

  return (
    <Card
      // The running node gets a ring rather than the old pulsing box-shadow: the
      // animation is #49's job, and a ring can't shift layout mid-stream.
      className={cn(
        "gap-0 py-4 transition-colors",
        active && "border-primary ring-primary/30 ring-2"
      )}
      // Machine-readable pipeline state, for the QA pass and for #49's transitions.
      data-agent={id}
      data-active={active || undefined}
      data-status={status ?? "idle"}
    >
      <CardContent className="px-4">
        <h3 className="text-sm leading-tight font-medium">{LABELS[id] ?? id}</h3>
        <Badge variant={statusVariant(latest?.status)} className="mt-2 uppercase">
          {status ?? "idle"}
        </Badge>
        {latest && (
          <p className="text-muted-foreground mt-2 text-xs leading-snug">{latest.detail}</p>
        )}
      </CardContent>
    </Card>
  );
}
