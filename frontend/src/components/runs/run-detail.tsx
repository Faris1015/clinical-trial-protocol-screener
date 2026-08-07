"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { AlertTriangle, ArrowLeft, Ban, ShieldCheck } from "lucide-react";
import { AgentCard } from "@/components/AgentCard";
import { ComplianceFindings } from "@/components/ComplianceFindings";
import { CriteriaProvenance } from "@/components/provenance/criteria-provenance";
import { CriteriaDiff } from "@/components/review/criteria-diff";
import { PatientMatchTable } from "@/components/PatientMatchTable";
import { ReportDownload } from "@/components/report-download";
import { RunTimeline } from "@/components/runs/run-timeline";
import { Reveal } from "@/components/motion";
import { RunDetailSkeleton } from "@/components/skeletons";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { apiFetch, problemDetail } from "@/lib/api";
import { formatTimestamp, statusLabel, statusVariant } from "@/lib/runs";
import type { AgentEvent, ScreeningState } from "@/types";

const AGENTS = ["router", "parser", "critic", "matcher"];

/**
 * Which agents' latest event to show on the pipeline cards. The event log is
 * append-only and one agent can appear several times (the critic→parser retry
 * loop), so the card reflects where that agent ended up.
 */
function latestEventPerAgent(events: AgentEvent[]): Record<string, AgentEvent> {
  const latest: Record<string, AgentEvent> = {};
  for (const entry of events) latest[entry.agent] = entry;
  return latest;
}

/**
 * One past run, replayed read-only from its checkpoint (#51).
 *
 * Everything here comes from `GET /api/screenings/{thread_id}/state`, which is
 * the graph's own durable state — so a run that finished last week renders from
 * exactly the data the pipeline produced, with no stream to reconnect to and
 * nothing to re-execute. There is deliberately no approve button: the live
 * screening page owns the gate, and offering it here would let a coordinator
 * think they were resuming a run when they were reading a transcript.
 */
export function RunDetail() {
  // `useSearchParams` rather than a `/runs/[threadId]` segment: the app is a
  // static export, so a dynamic segment would have to be enumerated at build
  // time (see lib/runs.runHref).
  const threadId = useSearchParams().get("id");
  const [state, setState] = useState<ScreeningState | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!threadId) return;
    let active = true;
    apiFetch(`/api/screenings/${encodeURIComponent(threadId)}/state`)
      .then(async (response) => {
        if (!active) return;
        if (!response.ok) {
          // 404 for an id that never existed (or a deleted database), 401 for an
          // expired session — the API's own wording is more useful than ours.
          setError(await problemDetail(response, "Could not load this run"));
          return;
        }
        setState((await response.json()) as ScreeningState);
      })
      .catch(() => {
        if (active) setError("Could not reach the server.");
      });
    return () => {
      active = false;
    };
  }, [threadId]);

  const backLink = (
    <Link
      href="/runs/"
      className="text-muted-foreground hover:text-foreground inline-flex items-center gap-1.5 text-sm"
    >
      <ArrowLeft className="size-4" aria-hidden="true" />
      Back to past runs
    </Link>
  );

  // A bookmark to /runs/view/ with no id, or a truncated paste of one.
  if (!threadId) {
    return (
      <div className="space-y-4">
        {backLink}
        <Card className="border-status-warn/40 bg-status-warn-soft" role="alert">
          <CardContent className="flex items-start gap-2.5 text-sm">
            <AlertTriangle className="text-status-warn mt-0.5 size-4 shrink-0" aria-hidden="true" />
            <span>This link is missing a run id. Pick a screening from the runs index.</span>
          </CardContent>
        </Card>
      </div>
    );
  }

  if (error) {
    return (
      <div className="space-y-4">
        {backLink}
        <Card className="border-destructive/40 bg-destructive/10" role="alert">
          <CardContent className="flex items-start gap-2.5 text-sm">
            <AlertTriangle className="text-destructive mt-0.5 size-4 shrink-0" aria-hidden="true" />
            <span>{error}</span>
          </CardContent>
        </Card>
      </div>
    );
  }

  if (!state) {
    // Shaped like the run it is standing in for (#49) — header, pipeline row,
    // criteria beside protocol — so the rehydrated run lands in the boxes the
    // reader is already looking at rather than shoving them down the page.
    return (
      <div className="space-y-4">
        {backLink}
        <RunDetailSkeleton />
      </div>
    );
  }

  const values = state.values;
  const events = values.events ?? [];
  const latest = latestEventPerAgent(events);
  const matches = values.matched_patients ?? [];
  const findings = values.compliance_findings ?? [];

  // Phase and filename come from the store row, not the checkpoint. A run that
  // was uploaded but never streamed has no checkpoint — `values` is `{}` — and
  // reading the phase out of it would render that run as a green "Done" while
  // the index it was opened from correctly says "Routing". `pending` still wins
  // when set, because it is the live "parked at the gate" signal.
  const record = state.screening;
  const phase =
    state.pending.length > 0 ? "awaiting_approval" : (record?.status ?? values.current_step ?? "");
  const filename = record?.source_filename ?? values.source_filename ?? "Screening";

  // No checkpoint and no events: the upload exists but the pipeline never ran
  // for it. Say so, rather than showing four blank agent cards.
  const neverRan = events.length === 0 && !values.parsed_criteria;

  return (
    // The whole replay fades up as one block (#49) rather than section by
    // section: `GET /state` fills every card below from the same response, so
    // staggering them would invent a sequence the data never had.
    <Reveal className="space-y-4" data-region="run-detail" data-phase={phase}>
      {backLink}

      <Card>
        <CardHeader>
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="min-w-0">
              <CardTitle className="flex flex-wrap items-center gap-2 text-base">
                {filename}
                {phase && <Badge variant={statusVariant(phase)}>{statusLabel(phase)}</Badge>}
              </CardTitle>
              <p className="text-muted-foreground font-mono text-xs break-all">
                {threadId}
                {record ? ` · uploaded ${formatTimestamp(record.created_at)}` : ""}
              </p>
            </div>
            {/* The export (#56) belongs on the header, not under the cohort: this
                is the handoff artifact for the whole run, and it is offered at
                every phase that produced something — a parked or escalated run's
                criteria and findings are exactly what gets handed to whoever has
                to act on it. Hidden only for a run that never streamed, which the
                API answers with a 409 anyway. */}
            {!neverRan && <ReportDownload threadId={threadId} size="sm" />}
          </div>
        </CardHeader>
      </Card>

      {neverRan ? (
        <Card data-region="run-never-started">
          <CardContent className="text-muted-foreground flex items-start gap-2.5 text-sm">
            <AlertTriangle className="text-status-warn mt-0.5 size-4 shrink-0" aria-hidden="true" />
            <span>
              This protocol was uploaded but its screening never ran, so there is nothing to replay.
            </span>
          </CardContent>
        </Card>
      ) : (
        <section className="grid grid-cols-2 gap-3 lg:grid-cols-4" aria-label="Pipeline">
          {AGENTS.map((id) => (
            <AgentCard
              key={id}
              id={id}
              // Nothing is running: this is a replay, so no card is ever the
              // active one.
              active={false}
              // AgentCard reads the last entry of `events`, so hand it just this
              // agent's final one rather than the whole interleaved log.
              state={
                latest[id] ? { status: "completed", update: { events: [latest[id]] } } : undefined
              }
            />
          ))}
        </section>
      )}

      {/* Provenance (#54): the criteria next to the protocol they were read out
          of, so an auditor replaying this run can check any one of them against
          the source passage rather than taking the extraction on trust. */}
      <CriteriaProvenance
        key={threadId}
        threadId={threadId}
        criteria={values.parsed_criteria ?? null}
      />

      {/* If a reviewer corrected this run's criteria (#53), the replay has to say
          so — the criteria above are theirs, not the parser's, and the cohort below
          was scored against them. Read back from the checkpoint, so it survives
          the session that made the edit. */}
      <CriteriaDiff edits={values.criteria_edits ?? []} />

      <ComplianceFindings findings={findings} summary={values.compliance_summary} />

      {/* The durable audit line (#50): patient data was only scored because a
          named reviewer authorized it. Read back from the checkpoint, so it
          survives long after the session that approved it. */}
      {values.approved_by && (
        <Card data-region="approval-provenance">
          <CardContent className="flex flex-wrap items-center gap-2 text-sm">
            <ShieldCheck className="text-primary size-4 shrink-0" aria-hidden="true" />
            <span>
              Patient matching authorized by{" "}
              <span className="font-medium">{values.approved_by}</span>
              {values.approved_by_role ? ` (${values.approved_by_role})` : ""}
              {values.approved_at ? ` on ${formatTimestamp(values.approved_at)}` : ""}.
            </span>
          </CardContent>
        </Card>
      )}

      {/* The gate's other durable decision (#91). Where the approval card
          attributes a cohort, this explains the absence of one: a rejected run's
          reason is the only thing that says why it stops here, and it has to
          outlive the session that typed it. */}
      {values.rejected_by && (
        <Card
          className="border-destructive/40 bg-destructive/10"
          data-region="rejection-provenance"
        >
          <CardContent className="flex items-start gap-2.5 text-sm">
            <Ban className="text-destructive mt-0.5 size-4 shrink-0" aria-hidden="true" />
            <span>
              <span className="font-medium">
                Rejected by {values.rejected_by}
                {values.rejected_by_role ? ` (${values.rejected_by_role})` : ""}
                {values.rejected_at ? ` on ${formatTimestamp(values.rejected_at)}` : ""}
              </span>
              . No patient data was matched.
              {values.rejected_reason && (
                <span className="block pt-1">{values.rejected_reason}</span>
              )}
            </span>
          </CardContent>
        </Card>
      )}

      <PatientMatchTable patients={matches} summary={values.match_summary} />

      {/* The audit view (#55): every agent transition in the order it happened,
          with the retry rounds numbered, the Critic's rejections named and the
          reviewer behind each human step attributed. Derived server-side, so this
          is the same trail the exported report prints. */}
      <RunTimeline timeline={state.timeline} />
    </Reveal>
  );
}
