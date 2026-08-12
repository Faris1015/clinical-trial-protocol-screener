"use client";

import Link from "next/link";
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
import { ViewModeToggle } from "@/components/view-mode-toggle";
import { useViewMode } from "@/hooks/useViewMode";
import { bucketOf, cohortVariant, type CohortBucket } from "@/lib/cohort";
import { patientHref } from "@/lib/patients";
import { cn } from "@/lib/utils";
import type { CriterionResult, PatientEvaluation } from "@/types";

/** The unresolved criteria — the technical layer's answer to "why not". */
function unresolved(e: PatientEvaluation): CriterionResult[] {
  return e.criterion_results.filter((r) => r.status !== "pass");
}

export function PatientMatchTable({
  patients,
  summary,
}: {
  patients: PatientEvaluation[];
  /** The cohort split in one plain-language line (#52); plain mode only. */
  summary?: string | null;
}) {
  const { technical } = useViewMode();
  if (!patients.length) return null;

  const counts = patients.reduce<Record<CohortBucket, number>>(
    (acc, e) => {
      acc[bucketOf(e)] += 1;
      return acc;
    },
    { eligible: 0, ineligible: 0, review: 0 }
  );

  return (
    <Card data-region="matches">
      <CardHeader>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle className="text-base">Cohort</CardTitle>
          <ViewModeToggle />
        </div>
        {/* Counts up front: the table can run to hundreds of rows, and the
            triage split is the thing a coordinator reads first. */}
        <div className="flex flex-wrap gap-1.5">
          <Badge variant="pass">{counts.eligible} eligible</Badge>
          <Badge variant="warn">{counts.review} need review</Badge>
          <Badge variant="fail">{counts.ineligible} ineligible</Badge>
        </div>
        {!technical && summary && <p className="text-sm">{summary}</p>}
      </CardHeader>
      <CardContent>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Patient</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>{technical ? "Failing / unknown criteria" : "Why"}</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {patients.map((e) => {
              const bucket = bucketOf(e);
              // Plain mode reads the matcher's own sentence for this patient —
              // which covers the "why not" for an excluded one. A run screened
              // before #52 has no summary, so it falls back to the chips rather
              // than showing a blank cell.
              const plain = !technical && Boolean(e.summary);
              return (
                <TableRow key={e.patient_id} data-bucket={bucket}>
                  {/* Through to the patient's own view (#96, AC 5) — the same
                      verdict this row shows, beside every other trial they have
                      been put to. The id is the link rather than the whole cell:
                      it is what identifies the record, and it is the shortest
                      target that still reads as one. */}
                  <TableCell className="align-top">
                    <Link
                      href={patientHref(e.patient_id)}
                      className="hover:text-primary font-mono text-xs underline-offset-4 hover:underline"
                    >
                      {e.patient_id}
                    </Link>
                    <span className="text-muted-foreground"> · </span>
                    {e.name}
                  </TableCell>
                  <TableCell className="align-top">
                    <Badge variant={cohortVariant(bucket)}>{bucket}</Badge>
                  </TableCell>
                  {/* Plain mode is prose, so it wraps: TableCell defaults to
                      `whitespace-nowrap`, which turns a full sentence into a
                      horizontal scroll. Technical mode keeps nowrap — the chips
                      truncate on purpose. */}
                  <TableCell className={cn("align-top", plain && "whitespace-normal")}>
                    {plain ? (
                      <div className="space-y-1">
                        <p>{e.summary}</p>
                        {/* One line per unresolved criterion, in the matcher's
                            plain wording — the detail behind the verdict without
                            making the reader switch layers. */}
                        {unresolved(e)
                          .filter((r) => r.explanation)
                          .map((r, i) => (
                            <p key={i} className="text-muted-foreground text-xs">
                              {r.explanation}
                            </p>
                          ))}
                      </div>
                    ) : (
                      <div className="flex flex-wrap gap-1">
                        {unresolved(e).map((r, i) => (
                          <Badge
                            key={i}
                            variant={r.status === "fail" ? "fail" : "warn"}
                            className="max-w-full"
                          >
                            <span className="truncate">
                              {r.criterion.source_text} ({r.status})
                            </span>
                          </Badge>
                        ))}
                      </div>
                    )}
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  );
}
