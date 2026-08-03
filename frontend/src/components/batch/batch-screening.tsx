"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { AlertTriangle, CheckCircle2, Layers } from "lucide-react";
import { Reveal } from "@/components/motion";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { isSettled, phaseLabel, runBatch, splitBatchItems, type BatchRun } from "@/lib/batch";
import { runHref, statusVariant } from "@/lib/runs";
import { cn } from "@/lib/utils";
import type { BatchCreated } from "@/types";

/**
 * A batch of screenings, running (#61).
 *
 * The multi-file counterpart to `ScreeningRun`: where that view follows one
 * protocol in full detail — criteria, provenance, the approval gate — this one
 * follows N protocols as far as the machine can take them without a human, which
 * is the approval gate. So it deliberately shows *less* per run: a phase, and a
 * link to the run itself. Rendering four pipelines and four criteria tables on one
 * page would be unreadable, and the reviewer's next step is one run at a time
 * anyway.
 *
 * The runs are already created (`POST /api/screenings/batch` minted a thread per
 * file); this drives their streams — `lib/batch` owns the queue and the phase
 * mapping — and keys everything by `thread_id`, so nothing here depends on the
 * files still being in the picker.
 */
export function BatchScreening({ created }: { created: BatchCreated }) {
  // `created` is fixed for this component's lifetime: the page remounts it per
  // submission (see app/page.tsx), which is the same "one run per mount" rule
  // ScreeningRun follows and what keeps a second batch from streaming into the
  // first one's rows.
  const initial = useMemo(() => splitBatchItems(created.items), [created]);
  const [runs, setRuns] = useState<BatchRun[]>(initial.runs);

  useEffect(() => {
    const controller = new AbortController();
    void runBatch(initial.runs, controller.signal, (threadId, phase, error) => {
      setRuns((previous) =>
        previous.map((run) =>
          run.thread_id === threadId
            ? // A later phase never carries the earlier phase's error forward: the
              // message belongs to the frame that reported it.
              { ...run, phase, error: error ?? null }
            : run
        )
      );
    });
    // Leaving the page cancels the streams. Each cancelled run keeps whatever the
    // pipeline had already checkpointed and can be re-run from its own page, which
    // is why the caption below asks the reader to stay put.
    return () => controller.abort();
  }, [initial.runs]);

  const settled = runs.filter((run) => isSettled(run.phase)).length;
  const waiting = runs.filter((run) => run.phase === "awaiting_approval").length;
  const done = settled === runs.length;

  return (
    <Reveal className="space-y-4" data-region="batch-screening" data-settled={settled}>
      {/* A submission every file of which was refused has no progress to show —
          only the rejections below, which would otherwise sit under an empty table
          reporting "0 of 0 finished". */}
      {runs.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <Layers className="text-muted-foreground size-4" aria-hidden="true" />
              Batch screening
            </CardTitle>
            {/* One live line rather than a per-row announcement: a batch changes
              phase every few seconds across every row, and a screen reader reading
              each of those would be unusable. */}
            <p className="text-muted-foreground text-xs" aria-live="polite">
              {settled} of {runs.length} finished
              {waiting > 0 && ` · ${waiting} awaiting approval`}
              {done ? "." : " · keep this page open while the rest run."}
            </p>
          </CardHeader>
          <CardContent className="space-y-4">
            {/* The tally above is the accessible version of this; the bar is a
              second encoding of it, so assistive tech skips it. */}
            <div className="bg-muted h-1.5 overflow-hidden rounded-full" aria-hidden="true">
              <div
                className={cn(
                  "bg-primary h-full rounded-full transition-[width] duration-500",
                  settled > 0 && "min-w-0.5"
                )}
                style={{ width: `${(settled / runs.length) * 100}%` }}
              />
            </div>

            <div className="overflow-x-auto">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Protocol</TableHead>
                    <TableHead>Status</TableHead>
                    <TableHead className="text-right">Run</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {runs.map((run) => (
                    <TableRow key={run.thread_id} data-status={run.phase}>
                      <TableCell className="font-medium">
                        {run.filename}
                        <span className="text-muted-foreground block font-mono text-xs">
                          {run.thread_id}
                        </span>
                      </TableCell>
                      <TableCell>
                        <Badge variant={statusVariant(run.phase)}>{phaseLabel(run.phase)}</Badge>
                        {run.error && (
                          <span className="text-muted-foreground mt-1 block text-xs">
                            {run.error}
                          </span>
                        )}
                      </TableCell>
                      <TableCell className="text-right">
                        {/* Every row is navigable the moment it exists, mid-run
                          included — the run's own page is where its criteria,
                          protocol and (for a parked run) the editor live. */}
                        <Link
                          href={runHref(run.thread_id)}
                          className="text-primary text-sm whitespace-nowrap underline-offset-4 hover:underline"
                        >
                          Open run
                        </Link>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>

            {done && (
              <p className="flex items-start gap-2 text-sm" data-region="batch-finished">
                <CheckCircle2 className="text-primary mt-0.5 size-4 shrink-0" aria-hidden="true" />
                <span>
                  Batch finished.{" "}
                  <Link href="/review/" className="text-primary underline-offset-4 hover:underline">
                    Review Queue
                  </Link>{" "}
                  lists the runs waiting on a person; every run is in{" "}
                  <Link href="/runs/" className="text-primary underline-offset-4 hover:underline">
                    Past Runs
                  </Link>
                  .
                </span>
              </p>
            )}
          </CardContent>
        </Card>
      )}

      {/* Files the server refused. Listed rather than folded into the table above:
          they have no run to open and no phase to reach, and a coordinator who
          dropped in a folder needs to see which documents did not make it. */}
      {initial.rejected.length > 0 && (
        <Card
          data-region="batch-rejected"
          role="alert"
          className="border-destructive/40 bg-destructive/10"
        >
          <CardContent className="space-y-2 text-sm">
            <p className="flex items-start gap-2.5">
              <AlertTriangle
                className="text-destructive mt-0.5 size-4 shrink-0"
                aria-hidden="true"
              />
              <span>
                {initial.rejected.length === 1
                  ? "One file could not be screened:"
                  : `${initial.rejected.length} files could not be screened:`}
              </span>
            </p>
            <ul className="ml-7 list-disc space-y-1">
              {initial.rejected.map((item, index) => (
                // Rejected files have no thread_id to key on, and two uploads can
                // share a name; the index is stable because this list never
                // changes after the submission that produced it.
                <li key={`${item.filename}-${index}`}>
                  <span className="font-medium">{item.filename}</span>
                  {item.detail ? ` — ${item.detail}` : ""}
                </li>
              ))}
            </ul>
          </CardContent>
        </Card>
      )}
    </Reveal>
  );
}
