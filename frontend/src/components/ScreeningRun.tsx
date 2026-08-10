"use client";

import { useState } from "react";
import Link from "next/link";
import { AnimatePresence } from "motion/react";
import { AlertTriangle, Ban, CheckCircle2, PencilLine } from "lucide-react";
import { useScreenerStream, type NodeState } from "@/hooks/useScreenerStream";
import { AgentCard } from "@/components/AgentCard";
import { Reveal } from "@/components/motion";
import { CohortSkeleton, CriteriaSkeleton } from "@/components/skeletons";
import { CriteriaProvenance } from "@/components/provenance/criteria-provenance";
import { PatientMatchTable } from "@/components/PatientMatchTable";
import { GateCoverage } from "@/components/review/gate-coverage";
import { RejectScreening } from "@/components/review/reject-screening";
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
 * Which agent is executing right now (#49) — the card that gets the ring and the
 * activity bar.
 *
 * This is not "the last agent we heard from". `stream_mode="updates"` only emits
 * once a node *finishes*, so the newest frame names the agent that just stopped;
 * the one actually working is the stage after it, and before any frame has
 * landed that is the router. Reading it the other way round left the whole
 * router phase — the first several seconds of every screening — with no card lit
 * at all, and then lit whichever agent had most recently finished.
 *
 * Two exceptions to "the next stage", both of them real graph behaviour:
 *  - the matcher reports while it is still working (its progress keepalives), so
 *    a non-terminal status means the sender is itself the running node;
 *  - a Critic rejection sends the graph back to the parser for another attempt
 *    rather than on to the matcher.
 */
function runningAgent(
  lastNode: string | null,
  nodeStates: Record<string, NodeState>
): string | null {
  if (!lastNode) return AGENTS[0];
  const events = nodeStates[lastNode]?.update.events ?? [];
  const status = events[events.length - 1]?.status;
  if (status === "started") return lastNode;
  if (status === "rejected") return "parser";
  const index = AGENTS.indexOf(lastNode);
  // An off-pipeline node (human_escalation) is terminal — nothing is running.
  if (index < 0) return null;
  return AGENTS[index + 1] ?? null;
}

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
  // The reason this run was stopped at the gate (#91), echoed locally like
  // `approvedBy`: the durable record is `rejected_reason` in graph state, written
  // by the API before it answers, and the run detail view reads it back.
  const [rejectedReason, setRejectedReason] = useState<string | null>(null);
  // True from the moment the approval is accepted until the matcher's stream
  // ends, which is what the cohort skeleton stands in for (#49). Tracked
  // separately from `phase`, which is back to "running" for the matcher exactly
  // as it was for the parse — the two waits look identical to the reducer but
  // need different placeholders.
  const [matching, setMatching] = useState(false);
  const { principal } = useAuth();
  const { nodeStates, lastNode, phase, setPhase, error, setError, applyFrame } =
    useScreenerStream(threadId);

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
    setMatching(true);
    // The matcher streams over SSE like the initial phase, but EventSource can't
    // POST — so the body is framed by hand (lib/sse, shared with the edit-and-rerun
    // page) and each frame goes through the same reducer the GET stream uses.
    try {
      await readEventStream(res, (msg) => {
        if (msg.node === "matcher" && msg.update?.matched_patients) {
          setMatches(msg.update.matched_patients);
          // The cohort's plain-language line (#52) rides the same frame as the
          // evaluations it summarizes, so they can never describe different runs.
          setMatchSummary(msg.update.match_summary ?? null);
        }
        return applyFrame(msg);
      });
    } finally {
      // In a `finally` so a stream that dies mid-cohort takes the skeleton down
      // with it — a placeholder left pulsing over a dead connection promises
      // patients that are never coming.
      setMatching(false);
    }
  }

  // Latest parsed criteria streamed from the parser node
  const parsed = nodeStates.parser?.update.parsed_criteria ?? null;
  const complianceSummary = nodeStates.critic?.update.compliance_summary ?? null;
  const activeAgent = phase === "running" ? runningAgent(lastNode, nodeStates) : null;

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

      <AnimatePresence>
        {phase === "failed" && (
          <Reveal key="banner-failed">
            <Card
              data-region="banner-failed"
              role="alert"
              className="border-status-warn/40 bg-status-warn-soft"
            >
              <CardContent className="flex flex-col gap-3 text-sm sm:flex-row sm:items-start">
                <AlertTriangle
                  className="text-status-warn mt-0.5 size-4 shrink-0"
                  aria-hidden="true"
                />
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
          </Reveal>
        )}
      </AnimatePresence>

      {/* The criteria beside the protocol they came from (#54) — the reviewer at
          the gate below is being asked to vouch for this extraction, so the
          passage behind each criterion is one click away.
          Until the parser answers, its skeleton holds the place (#49): the
          router and the parser are both model calls, so this is the longest
          blank stretch of a live run. `mode="wait"` because the two are the same
          box — the placeholder leaves before the real card arrives, instead of
          the page briefly showing both. */}
      <AnimatePresence mode="wait" initial={false}>
        {parsed ? (
          <Reveal key="criteria">
            <CriteriaProvenance key={threadId} threadId={threadId} criteria={parsed} />
          </Reveal>
        ) : phase === "running" ? (
          <Reveal key="criteria-skeleton">
            <CriteriaSkeleton />
          </Reveal>
        ) : null}
      </AnimatePresence>

      {/* What this run could actually screen on (#93), between the criteria and
          the gate that authorizes matching them. A reviewer being asked to approve
          an extraction that dropped six sentences should read that here, not in the
          report afterwards — and the figure is the server's own, so it is the same
          number the run detail view will show. */}
      {threadId && phase === "awaiting_approval" && <GateCoverage threadId={threadId} />}

      <AnimatePresence>
        {phase === "awaiting_approval" && (
          <Reveal key="banner-approval">
            <Card data-region="banner-approval" className="border-primary/40 bg-primary/10">
              {/* Wraps, unlike the other banners: the reject form (#91) opens
                  inline as a full-width block, and without a wrap it would try to
                  share a row with the approve button. */}
              <CardContent className="flex flex-col gap-3 text-sm sm:flex-row sm:flex-wrap sm:items-center">
                <CheckCircle2 className="text-primary size-4 shrink-0" aria-hidden="true" />
                <span className="flex-1">
                  Compliance checks passed. Review the criteria above, then approve patient matching
                  — or correct them first if the extraction is wrong. If the protocol cannot be
                  screened at all, reject it and say why.
                </span>
                {editorLink}
                {/* The gate's three exits, in the order a reviewer weighs them:
                    correct the extraction, refuse the protocol, or authorize
                    matching. Reject sits beside approve rather than behind the
                    editor link (#91) — a run nobody can screen should not have to
                    be edited before it can be stopped. */}
                {threadId && (
                  <RejectScreening
                    threadId={threadId}
                    onRejected={(reason) => {
                      setRejectedReason(reason);
                      setPhase("rejected");
                    }}
                  />
                )}
                <Button onClick={approve} size="lg" className="shrink-0">
                  Approve → run matching
                </Button>
              </CardContent>
            </Card>
          </Reveal>
        )}
      </AnimatePresence>

      {/* The decision, stated where the gate used to be (#91). Terminal: there is
          no cohort coming and nothing left to act on here, so this replaces the
          banner rather than sitting under it. */}
      <AnimatePresence>
        {phase === "rejected" && (
          <Reveal key="banner-rejected">
            <Card
              data-region="banner-rejected"
              role="status"
              className="border-destructive/40 bg-destructive/10"
            >
              <CardContent className="flex flex-col gap-2 text-sm">
                <span className="flex items-center gap-2.5 font-medium">
                  <Ban className="text-destructive size-4 shrink-0" aria-hidden="true" />
                  Screening rejected{principal?.email ? ` by ${principal.email}` : ""}. No patient
                  data was matched.
                </span>
                {rejectedReason && <span className="pl-6.5">{rejectedReason}</span>}
              </CardContent>
            </Card>
          </Reveal>
        )}
      </AnimatePresence>

      {/* Audit trail: patient data was only touched because a named reviewer
          authorized it (#50). Rendered with the cohort, so results are never shown
          without their attribution. */}
      {approvedBy && (
        <p className="text-muted-foreground text-xs" data-region="approval-provenance">
          Patient matching authorized by{" "}
          <span className="text-foreground font-medium">{approvedBy}</span>.
        </p>
      )}

      {/* The cohort, or the shape of it while the matcher is still scoring —
          one evaluation per patient, so this is the wait the reviewer has just
          explicitly asked for by approving the gate. */}
      <AnimatePresence mode="wait" initial={false}>
        {matches.length > 0 ? (
          <Reveal key="cohort">
            <PatientMatchTable patients={matches} summary={matchSummary} />
          </Reveal>
        ) : matching ? (
          <Reveal key="cohort-skeleton">
            <CohortSkeleton />
          </Reveal>
        ) : null}
      </AnimatePresence>

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
