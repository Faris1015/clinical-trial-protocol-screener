"use client";

import { AnimatePresence, m, useReducedMotion } from "motion/react";
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

/**
 * The "this stage is working" indicator (#49): a sliver sweeping the card's
 * bottom edge, so the handoff from one agent to the next is visible as movement
 * travelling across the pipeline rather than as a ring silently relocating.
 *
 * Absolutely positioned on a card that already clips its overflow, so it can
 * never affect layout — which matters on a grid whose cards are still growing
 * their detail text as frames arrive.
 *
 * It loops for as long as the node runs, which is precisely what
 * `prefers-reduced-motion` is asking us not to do, so reduced motion gets the
 * same bar holding still: the card is still marked as the running one, just
 * without the travel. This is the one place that branches on the setting rather
 * than leaving it to `MotionConfig` — a loop dropped to "no transform" would
 * otherwise park the sliver in the corner and read as a rendering bug.
 */
function ActivityBar() {
  const reduced = useReducedMotion();
  if (reduced) {
    return <span aria-hidden="true" className="bg-primary/50 absolute inset-x-0 bottom-0 h-0.5" />;
  }
  return (
    <m.span
      aria-hidden="true"
      className="bg-primary absolute bottom-0 left-0 h-0.5 w-1/3 rounded-full"
      animate={{ x: ["-110%", "310%"] }}
      transition={{ duration: 1.6, repeat: Infinity, ease: "easeInOut" }}
    />
  );
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
      // The running node gets a ring rather than the old pulsing box-shadow: a
      // ring can't shift layout mid-stream. `relative` anchors the activity bar.
      className={cn(
        "relative gap-0 py-4 transition-colors",
        active && "border-primary ring-primary/30 ring-2"
      )}
      // Machine-readable pipeline state, for the QA pass and for #49's transitions.
      data-agent={id}
      data-active={active || undefined}
      data-status={status ?? "idle"}
    >
      <CardContent className="px-4">
        <h3 className="text-sm leading-tight font-medium">{LABELS[id] ?? id}</h3>
        {/* Keyed on the status, not on the detail text: the matcher emits a
            progress frame per patient, and cross-fading the block on each of
            those would strobe. A status change is the actual handoff, and it is
            the only thing worth animating. `mode="wait"` keeps the outgoing and
            incoming copies from ever being stacked in the same box. */}
        <AnimatePresence mode="wait" initial={false}>
          <m.div
            key={status ?? "idle"}
            initial={{ opacity: 0, y: 4 }}
            animate={{ opacity: 1, y: 0 }}
            // Faster out than in, and both short: `mode="wait"` runs them back
            // to back, so their sum is how far the card lags the pipeline.
            exit={{ opacity: 0, y: -4, transition: { duration: 0.1, ease: "easeIn" } }}
            transition={{ duration: 0.16, ease: "easeOut" }}
          >
            <Badge variant={statusVariant(latest?.status)} className="mt-2 uppercase">
              {status ?? "idle"}
            </Badge>
            {latest && (
              <p className="text-muted-foreground mt-2 text-xs leading-snug">{latest.detail}</p>
            )}
          </m.div>
        </AnimatePresence>
      </CardContent>
      {active && <ActivityBar />}
    </Card>
  );
}
