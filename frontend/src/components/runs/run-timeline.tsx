import { History, PencilLine, UserCheck } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import { formatTime, formatTimestamp } from "@/lib/runs";
import type { RunTimeline as RunTimelineData, TimelineEntry, TimelineSummary } from "@/types";

/**
 * Which badge variant a step's outcome reads as. `rejected`/`escalated` are the
 * Critic pushing work back and `edited` is a reviewer correcting it — neither is
 * a failure, and only `failed` is terminal-bad. Mirrors the report's
 * `_OUTCOME_TAGS` (backend/app/services/report.py).
 */
function outcomeVariant(status: string): "pass" | "fail" | "warn" | "secondary" {
  switch (status) {
    case "completed":
    case "approved":
      return "pass";
    case "failed":
      return "fail";
    case "rejected":
    case "escalated":
    case "edited":
      return "warn";
    default:
      return "secondary";
  }
}

/** The rail marker, coloured from the same mapping the badge uses. */
const DOT_CLASSES: Record<string, string> = {
  pass: "bg-status-pass",
  fail: "bg-status-fail",
  warn: "bg-status-warn",
  secondary: "bg-muted-foreground",
};

/**
 * The run's shape in numbers, above the entries.
 *
 * Only the facts that say something: a run that never looped, was never edited
 * and never escalated is the normal case, and a row of zeros would bury the one
 * figure that matters (how long it took) in noise.
 */
function summaryFacts(summary?: TimelineSummary): string[] {
  const facts: string[] = [];
  // Optional for the same reason `timeline` itself is: a payload from an older
  // build must cost this line, not the whole run detail page.
  if (!summary) return facts;
  // The date, stated once — every entry below shows the time of day only.
  if (summary.started_at) facts.push(formatTimestamp(summary.started_at));
  if (summary.duration) facts.push(`Ran in ${summary.duration}`);
  if (summary.attempts > 1) facts.push(`${summary.attempts} extraction attempts`);
  if (summary.critic_rejections > 0) {
    facts.push(`${summary.critic_rejections} Critic rejection${plural(summary.critic_rejections)}`);
  }
  if (summary.revisions > 0) {
    facts.push(`${summary.revisions} reviewer revision${plural(summary.revisions)}`);
  }
  if (summary.escalated) facts.push("Escalated for human review");
  // Read from the durable approval trail rather than from an entry (#50), so a
  // checkpoint whose approval event is missing still attributes the matching.
  if (summary.approved_by) facts.push(`Authorized by ${summary.approved_by}`);
  return facts;
}

function plural(count: number): string {
  return count === 1 ? "" : "s";
}

function ActorLine({ entry }: { entry: TimelineEntry }) {
  const Icon = entry.status === "edited" ? PencilLine : UserCheck;
  return (
    <p className="text-muted-foreground flex items-center gap-1.5 text-xs">
      <Icon className="size-3.5 shrink-0" aria-hidden="true" />
      <span className="text-foreground font-medium">{entry.actor}</span>
      {entry.actor_role && <span>({entry.actor_role})</span>}
    </p>
  );
}

/**
 * One run's event log as a chronological audit trail (#55).
 *
 * The graph records every transition in an append-only list, which read raw is a
 * flat sequence of sentences. What an auditor asks of a run is narrower: how many
 * times the Parser had to try, what the Critic pushed back, whether it escalated,
 * and who authorized touching patient data. So each step carries its retry round,
 * the gap since the step before it, and — for a human step — the identity behind
 * it, with the headline figures summarized above.
 *
 * `timeline` is derived and rendered server-side (`app/services/timeline.py`):
 * the labels, the attempt numbers and the elapsed gaps arrive already resolved,
 * which is what lets the downloadable report (#56) print the same trail without a
 * second implementation of the derivation. Renders nothing for a run that never
 * streamed — the detail view says so once, in its own notice.
 *
 * Deliberately not the only place the approver appears: the approval card above
 * the cohort (#50) attributes the *results*, this attributes the *moment*. A
 * reader scanning for either finds it where they looked.
 */
export function RunTimeline({ timeline }: { timeline?: RunTimelineData }) {
  const entries = timeline?.entries ?? [];
  if (!timeline || entries.length === 0) return null;

  const facts = summaryFacts(timeline.summary);
  // Number the retry rounds only for a run that actually looped: on the common
  // single-attempt run, "attempt 1" on two rows is noise rather than provenance.
  const numbered = (timeline.summary?.attempts ?? 0) > 1;

  return (
    <Card data-region="run-timeline">
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <History className="text-muted-foreground size-4" aria-hidden="true" />
          Event timeline
        </CardTitle>
        {facts.length > 0 && (
          <p className="text-muted-foreground text-xs" data-region="run-timeline-summary">
            {facts.join(" · ")}
          </p>
        )}
      </CardHeader>
      <CardContent>
        {/* An ordered list, because the order *is* the content: a screen reader
            reads "3 of 8" and gets the same sequence the rail draws. */}
        <ol>
          {entries.map((entry, index) => {
            const variant = outcomeVariant(entry.status);
            const last = index === entries.length - 1;
            return (
              <li
                key={entry.seq}
                className="flex gap-3"
                // Machine-readable, for the QA pass and for anything asserting on
                // a specific step of a specific run.
                data-agent={entry.agent}
                data-status={entry.status}
                data-attempt={entry.attempt || undefined}
              >
                <div className="flex flex-col items-center" aria-hidden="true">
                  <span className={cn("mt-1.5 size-2.5 rounded-full", DOT_CLASSES[variant])} />
                  {/* Grows to the row's height, so the rail is continuous whatever
                      a step's detail text wraps to. The gap between steps is the
                      *content* column's padding, not the row's — put it on the row
                      and the rail would break once per step. */}
                  {!last && <span className="bg-border mt-1 w-0.5 flex-1 rounded-full" />}
                </div>
                <div className={cn("min-w-0 flex-1 space-y-1", !last && "pb-4")}>
                  <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
                    <span className="text-sm font-medium">{entry.label}</span>
                    <Badge variant={variant}>{entry.outcome}</Badge>
                    {numbered && entry.attempt > 0 && (
                      <Badge variant="outline">attempt {entry.attempt}</Badge>
                    )}
                    {entry.revision > 0 && (
                      <Badge variant="outline">revision {entry.revision}</Badge>
                    )}
                    <span className="text-muted-foreground ml-auto shrink-0 text-xs tabular-nums">
                      {entry.elapsed && <span className="mr-2">{entry.elapsed}</span>}
                      {entry.timestamp ? formatTime(entry.timestamp) : ""}
                    </span>
                  </div>
                  <p className="text-sm">{entry.detail}</p>
                  {entry.actor && <ActorLine entry={entry} />}
                </div>
              </li>
            );
          })}
        </ol>
      </CardContent>
    </Card>
  );
}
