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
import type { PatientEvaluation } from "@/types";

type Bucket = "eligible" | "ineligible" | "review";

/**
 * `needs_review` outranks `eligible`: a patient the matcher could not fully
 * determine must reach a human, even if every criterion it *could* evaluate passed.
 */
function bucketOf(e: PatientEvaluation): Bucket {
  if (e.needs_review) return "review";
  return e.eligible ? "eligible" : "ineligible";
}

const BUCKET_VARIANT = {
  eligible: "pass",
  ineligible: "fail",
  review: "warn",
} as const;

export function PatientMatchTable({ patients }: { patients: PatientEvaluation[] }) {
  if (!patients.length) return null;

  const counts = patients.reduce<Record<Bucket, number>>(
    (acc, e) => {
      acc[bucketOf(e)] += 1;
      return acc;
    },
    { eligible: 0, ineligible: 0, review: 0 }
  );

  return (
    <Card data-region="matches">
      <CardHeader>
        <CardTitle className="text-base">Cohort</CardTitle>
        {/* Counts up front: the table can run to hundreds of rows, and the
            triage split is the thing a coordinator reads first. */}
        <div className="flex flex-wrap gap-1.5">
          <Badge variant="pass">{counts.eligible} eligible</Badge>
          <Badge variant="warn">{counts.review} need review</Badge>
          <Badge variant="fail">{counts.ineligible} ineligible</Badge>
        </div>
      </CardHeader>
      <CardContent>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Patient</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>Failing / unknown criteria</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {patients.map((e) => {
              const bucket = bucketOf(e);
              return (
                <TableRow key={e.patient_id} data-bucket={bucket}>
                  <TableCell className="align-top">
                    <span className="font-mono text-xs">{e.patient_id}</span>
                    <span className="text-muted-foreground"> · </span>
                    {e.name}
                  </TableCell>
                  <TableCell className="align-top">
                    <Badge variant={BUCKET_VARIANT[bucket]}>{bucket}</Badge>
                  </TableCell>
                  <TableCell className="align-top">
                    <div className="flex flex-wrap gap-1">
                      {e.criterion_results
                        .filter((r) => r.status !== "pass")
                        .map((r, i) => (
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
