"use client";

import { useState } from "react";
import Link from "next/link";
import { AlertTriangle, CheckCircle2, PencilLine } from "lucide-react";
import { useScreenerStream } from "@/hooks/useScreenerStream";
import { AgentCard } from "@/components/AgentCard";
import { CriteriaProvenance } from "@/components/provenance/criteria-provenance";
import { PatientMatchTable } from "@/components/PatientMatchTable";
import { ReportDownload } from "@/components/report-download";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { useAuth } from "@/components/AuthProvider";
import { apiFetch, problemDetail } from "@/lib/api";
import { reviewHref } from "@/lib/criteria";
import { readEventStream } from "@/lib/sse";
import type { PatientEvaluation } from "@/types";

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
  const [matchSummary, setMatchSummary] = useState<string | null>(null);
  const [approvedBy, setApprovedBy] = useState<string | null>(null);
  const { principal } = useAuth();
  const { nodeStates, phase, setPhase, error, setError, applyFrame } = useScreenerStream(threadId);

  async function approve() {
    // Flip to "running" first: it hides the approval banner (and its button),
    // so a slow matcher can't be double-approved into a duplicate run.
    setError(null);
    setPhase("running");
    const res = await apiFetch(`/api/screenings/${encodeURIComponent(threadId!)}/approve`, {
      method: "POST",
    });
    if (!res.ok || !res.body) {
      // Eager-validation errors (404 unknown thread, 409 not at the gate, 429
      // slots full, 401 expired session) arrive as JSON before the stream commits
      // — the screening stays parked at the gate, so show the error instead of
      // hanging. A 401 also trips apiFetch's handler, which sends the user to the
      // login page; the screening is still at the gate when they come back.
      setError(await problemDetail(res, "Approval failed"));
      setPhase("failed");
      return;
    }
    // Only now — the request was accepted, so the server has stamped the approver
    // into the checkpoint. Setting it before the call would leave a rejected
    // approval (409, expired session) claiming on screen that someone authorized
    // patient matching, which is exactly the wrong thing for an audit line to do.
    //
    // The durable record is the server's (`approved_by` in graph state, #50);
    // this is its local echo, since that write lands before the resume and so
    // never appears in the SSE stream. #55's audit view reads it back via /state.
    setApprovedBy(principal?.email ?? null);
    // The matcher streams over SSE like the initial phase, but EventSource can't
    // POST — so the body is framed by hand (lib/sse, shared with the edit-and-rerun
    // page) and each frame goes through the same reducer the GET stream uses.
    await readEventStream(res, (msg) => {
      if (msg.node === "matcher" && msg.update?.matched_patients) {
        setMatches(msg.update.matched_patients);
        // The cohort's plain-language line (#52) rides the same frame as the
        // evaluations it summarizes, so they can never describe different runs.
        setMatchSummary(msg.update.match_summary ?? null);
      }
      return applyFrame(msg);
    });
  }

  // Latest parsed criteria streamed from the parser node
  const parsed = nodeStates.parser?.update.parsed_criteria ?? null;
  const complianceSummary = nodeStates.critic?.update.compliance_summary ?? null;
  const activeAgent =
    phase === "running" ? ([...AGENTS].reverse().find((a) => nodeStates[a]) ?? null) : null;

  // Route to the editor (#53) from both human-facing stops — the gate and the
  // blocked/escalated path — rather than only from a failure. Requires a parsed
  // extraction and a thread to edit; without either there is nothing to fix.
  const editorLink =
    threadId && parsed ? (
      <Link
        href={reviewHref(threadId)}
        className="text-primary inline-flex shrink-0 items-center gap-1.5 text-sm whitespace-nowrap underline-offset-4 hover:underline"
        data-region="edit-criteria-link"
      >
        <PencilLine className="size-3.5" aria-hidden="true" />
        Edit criteria & re-run
      </Link>
    ) : null;

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
          <CardContent className="flex flex-col gap-3 text-sm sm:flex-row sm:items-start">
            <AlertTriangle className="text-status-warn mt-0.5 size-4 shrink-0" aria-hidden="true" />
            <span className="flex-1">
              {error ?? "Could not converge — escalated to human review after 3 attempts."}
              {/* What the Critic actually objected to, in plain language (#52):
                  a reviewer heading for the editor needs to know what to fix,
                  and "could not converge" alone does not say. Only for a
                  compliance escalation — a stream `error` is a different story. */}
              {!error && complianceSummary && (
                <span className="block pt-1">{complianceSummary}</span>
              )}
            </span>
            {/* The escalation exit (#53). Offered only when there is an extraction
                to correct: a run that died in the router or the parser has nothing
                for a reviewer to edit, and linking there would be a dead end. */}
            {editorLink}
          </CardContent>
        </Card>
      )}

      {/* The criteria beside the protocol they came from (#54) — the reviewer at
          the gate below is being asked to vouch for this extraction, so the
          passage behind each criterion is one click away. */}
      {parsed && <CriteriaProvenance key={threadId} threadId={threadId} criteria={parsed} />}

      {phase === "awaiting_approval" && (
        <Card data-region="banner-approval" className="border-primary/40 bg-primary/10">
          <CardContent className="flex flex-col gap-3 text-sm sm:flex-row sm:items-center">
            <CheckCircle2 className="text-primary size-4 shrink-0" aria-hidden="true" />
            <span className="flex-1">
              Compliance checks passed. Review the criteria above, then approve patient matching —
              or correct them first if the extraction is wrong.
            </span>
            {editorLink}
            <Button onClick={approve} size="lg" className="shrink-0">
              Approve → run matching
            </Button>
          </CardContent>
        </Card>
      )}

      {/* Audit trail: patient data was only touched because a named reviewer
          authorized it (#50). Rendered with the cohort, so results are never shown
          without their attribution. */}
      {approvedBy && (
        <p className="text-muted-foreground text-xs" data-region="approval-provenance">
          Patient matching authorized by{" "}
          <span className="text-foreground font-medium">{approvedBy}</span>.
        </p>
      )}

      {matches.length > 0 && <PatientMatchTable patients={matches} summary={matchSummary} />}

      {/* Offered here only once the run has finished (#56). The report is built
          server-side from the checkpoint, so exporting mid-run would hand a
          reviewer a document that omits whatever the pipeline wrote in the
          seconds after they clicked — and the run detail view is where a
          partially-completed run gets exported from, with its phase stated on the
          page. */}
      {threadId && phase === "done" && <ReportDownload threadId={threadId} />}
    </div>
  );
}
