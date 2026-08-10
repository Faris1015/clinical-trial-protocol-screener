"use client";

import { useEffect, useState } from "react";
import { AlertTriangle } from "lucide-react";
import { CoveragePanel } from "@/components/runs/coverage-panel";
import { Card, CardContent } from "@/components/ui/card";
import { apiFetch } from "@/lib/api";
import type { ScreeningState } from "@/types";

/**
 * The coverage figure at the approval gate (#93) — "we could only check 14 of 20
 * criteria", while the decision is still the reviewer's.
 *
 * This is the placement the score exists for. Everywhere else it is a fact about a
 * run that already happened; here it is an input to a decision, and a reviewer
 * approving patient matching on an extraction that dropped six sentences should be
 * told so before they click rather than discover it in the exported report.
 *
 * Fetched from `GET /api/screenings/{id}/state` rather than computed from the
 * criteria the parser streamed. Two reasons, and the second is the important one:
 * the derivation is the server's (`backend/app/services/coverage.py`), so the
 * number at the gate is byte-for-byte the number the run detail view and the report
 * will show; and a run parked at the gate has a checkpoint, which is exactly what
 * that endpoint reads. Recounting the buckets in the browser would be a second
 * implementation of the one rule that must not drift.
 *
 * Mounted only while the gate is open, so the fetch happens when the gate opens and
 * again if a reviewer's edit sends the run back round to it.
 */
export function GateCoverage({ threadId }: { threadId: string }) {
  const [state, setState] = useState<ScreeningState | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let active = true;
    apiFetch(`/api/screenings/${encodeURIComponent(threadId)}/state`)
      .then(async (response) => {
        if (!active) return;
        if (!response.ok) {
          setFailed(true);
          return;
        }
        setState((await response.json()) as ScreeningState);
      })
      .catch(() => {
        if (active) setFailed(true);
      });
    return () => {
      active = false;
    };
  }, [threadId]);

  if (failed) {
    // Said out loud rather than rendered as nothing. An absent panel is
    // indistinguishable from "nothing was missed", and that is the one reading a
    // reviewer must not take from a failed request at this gate.
    return (
      <Card
        className="border-status-warn/40 bg-status-warn-soft"
        data-region="coverage-unavailable"
      >
        <CardContent className="flex items-start gap-2.5 text-sm">
          <AlertTriangle className="text-status-warn mt-0.5 size-4 shrink-0" aria-hidden="true" />
          <span>
            Could not load how much of this protocol is checkable. The criteria above are still what
            this run would screen on — check them for anything the extraction left out.
          </span>
        </CardContent>
      </Card>
    );
  }

  // Nothing while the request is in flight: the panel is one card among several
  // that arrive together, and a skeleton for a figure the reviewer is not yet
  // waiting on would draw the eye away from the criteria they are reading.
  return <CoveragePanel coverage={state?.coverage} />;
}
