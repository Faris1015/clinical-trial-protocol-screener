"use client";

import { useState } from "react";
import { AlertTriangle, CheckCircle2 } from "lucide-react";
import { useScreenerStream } from "@/hooks/useScreenerStream";
import { AgentCard } from "@/components/AgentCard";
import { CriteriaTable } from "@/components/CriteriaTable";
import { PatientMatchTable } from "@/components/PatientMatchTable";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import type { PatientEvaluation, StreamMessage } from "@/types";

const AGENTS = ["router", "parser", "critic", "matcher"];

/**
 * One screening: the live pipeline, the parsed criteria, the approval gate and
 * the resulting cohort. All of its state belongs to a single `threadId`, so the
 * caller mounts it under `key={threadId}` and a new upload gets a clean instance
 * instead of a hand-rolled reset (see useScreenerStream).
 *
 * `threadId` is null before the first upload — the idle pipeline still renders,
 * it just has no stream to subscribe to.
 */
export function ScreeningRun({ threadId }: { threadId: string | null }) {
  const [matches, setMatches] = useState<PatientEvaluation[]>([]);
  const { nodeStates, phase, setPhase, error, setError, applyFrame } = useScreenerStream(threadId);

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
    <div className="space-y-4" data-phase={phase}>
      {/* One column per agent on desktop; two on phones, where four would crush
          the detail text to unreadable width. */}
      <section className="grid grid-cols-2 gap-3 lg:grid-cols-4" aria-label="Pipeline">
        {AGENTS.map((id) => (
          <AgentCard key={id} id={id} active={id === activeAgent} state={nodeStates[id]} />
        ))}
      </section>

      {phase === "failed" && (
        <Card
          data-region="banner-failed"
          role="alert"
          className="border-status-warn/40 bg-status-warn-soft"
        >
          <CardContent className="flex items-start gap-2.5 text-sm">
            <AlertTriangle className="text-status-warn mt-0.5 size-4 shrink-0" aria-hidden="true" />
            <span>
              {error ?? "Could not converge — escalated to human review after 3 attempts."}
            </span>
          </CardContent>
        </Card>
      )}

      {parsed && <CriteriaTable criteria={parsed} />}

      {phase === "awaiting_approval" && (
        <Card data-region="banner-approval" className="border-primary/40 bg-primary/10">
          <CardContent className="flex flex-col gap-3 text-sm sm:flex-row sm:items-center">
            <CheckCircle2 className="text-primary size-4 shrink-0" aria-hidden="true" />
            <span className="flex-1">
              Compliance checks passed. Review the criteria above, then approve patient matching.
            </span>
            <Button onClick={approve} size="lg" className="shrink-0">
              Approve → run matching
            </Button>
          </CardContent>
        </Card>
      )}

      {matches.length > 0 && <PatientMatchTable patients={matches} />}
    </div>
  );
}
