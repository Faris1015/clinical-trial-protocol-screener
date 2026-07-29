"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { AlertTriangle, ClipboardCheck, PencilLine } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { apiFetch, problemDetail } from "@/lib/api";
import { REVIEW_STATUSES, reviewHref } from "@/lib/criteria";
import { formatTimestamp, runHref, statusLabel, statusVariant } from "@/lib/runs";
import type { Screening, ScreeningPage } from "@/types";

// Enough to hold a realistic backlog on one screen. The queue is bounded by how
// many runs are waiting on a person, not by how many have ever run — that is what
// the paged runs index (#51) is for.
const PAGE_SIZE = 100;

/**
 * The reviewer's worklist (#53): every run waiting on a human, in one place.
 *
 * `GET /api/screenings` filters by a single status, so this fetches each of the
 * three queue-worthy ones (see REVIEW_STATUSES) and merges them newest-first,
 * rather than asking the API for a multi-status filter that nothing else needs. A
 * failed fetch of one status doesn't blank the others.
 *
 * Each row links to the criteria editor, which is what makes the escalated and
 * failed runs actionable at all — before this they were visible on the runs index
 * and impossible to move.
 */
export function ReviewQueue() {
  const [rows, setRows] = useState<Screening[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;

    async function loadStatus(status: string): Promise<Screening[]> {
      const params = new URLSearchParams({ limit: String(PAGE_SIZE), offset: "0", status });
      const response = await apiFetch(`/api/screenings?${params}`);
      if (!response.ok) throw new Error(await problemDetail(response, "Could not load the queue"));
      return ((await response.json()) as ScreeningPage).items;
    }

    Promise.allSettled(REVIEW_STATUSES.map(loadStatus))
      .then((results) => {
        if (!active) return;
        const loaded = results.flatMap((r) => (r.status === "fulfilled" ? r.value : []));
        const failures = results.filter((r) => r.status === "rejected");
        // Dedupe by thread_id: the three requests are not one snapshot, so a run
        // that moves (awaiting_approval → failed, say) while they are in flight can
        // come back in two of them — rendering the same run twice under a duplicate
        // React key.
        const unique = [...new Map(loaded.map((run) => [run.thread_id, run])).values()];
        setRows(unique.sort((a, b) => b.created_at.localeCompare(a.created_at)));
        // Partial failure is worth saying out loud: a silently short queue reads
        // as "nothing to review", which is the one wrong conclusion here.
        setError(
          failures.length === 0
            ? null
            : failures.length === REVIEW_STATUSES.length
              ? "Could not load the review queue."
              : "Part of the queue could not be loaded — some runs may be missing."
        );
      })
      .catch(() => {
        if (active) setError("Could not reach the server.");
      });
    return () => {
      active = false;
    };
  }, []);

  return (
    <div className="space-y-4" data-region="review-queue">
      {error && (
        <Card className="border-destructive/40 bg-destructive/10" role="alert">
          <CardContent className="flex items-start gap-2.5 text-sm">
            <AlertTriangle className="text-destructive mt-0.5 size-4 shrink-0" aria-hidden="true" />
            <span>{error}</span>
          </CardContent>
        </Card>
      )}

      <Card>
        <CardContent>
          {rows === null ? (
            <div className="space-y-2" aria-hidden="true">
              <Skeleton className="h-8 w-full" />
              <Skeleton className="h-8 w-full" />
              <Skeleton className="h-8 w-5/6" />
            </div>
          ) : rows.length === 0 ? (
            <div
              className="text-muted-foreground flex flex-col items-center gap-2 py-8 text-center text-sm"
              data-region="review-empty"
            >
              <ClipboardCheck className="size-5" aria-hidden="true" />
              <span>
                Nothing waiting on a reviewer.{" "}
                <Link href="/runs/" className="text-primary underline-offset-4 hover:underline">
                  Past runs
                </Link>{" "}
                has everything that already finished.
              </span>
            </div>
          ) : (
            <div className="overflow-x-auto">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Protocol</TableHead>
                    <TableHead>Waiting on</TableHead>
                    <TableHead className="text-right">Criteria</TableHead>
                    <TableHead>Started</TableHead>
                    <TableHead className="text-right">Action</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {rows.map((run) => (
                    <TableRow key={run.thread_id} data-status={run.status}>
                      <TableCell className="font-medium">
                        <Link
                          href={runHref(run.thread_id)}
                          className="hover:text-primary underline-offset-4 hover:underline"
                        >
                          {run.source_filename}
                        </Link>
                        <span className="text-muted-foreground block font-mono text-xs">
                          {run.thread_id}
                        </span>
                      </TableCell>
                      <TableCell>
                        <Badge variant={statusVariant(run.status)}>{statusLabel(run.status)}</Badge>
                      </TableCell>
                      <TableCell className="text-right tabular-nums">
                        {run.criteria_count}
                      </TableCell>
                      <TableCell className="text-muted-foreground whitespace-nowrap">
                        {formatTimestamp(run.created_at)}
                      </TableCell>
                      <TableCell className="text-right">
                        <Link
                          href={reviewHref(run.thread_id)}
                          className="text-primary inline-flex items-center gap-1.5 text-sm whitespace-nowrap underline-offset-4 hover:underline"
                        >
                          <PencilLine className="size-3.5" aria-hidden="true" />
                          Review criteria
                        </Link>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
