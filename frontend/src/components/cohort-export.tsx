"use client";

import { useState } from "react";
import { Loader2, Sheet } from "lucide-react";
import { Button } from "@/components/ui/button";
import { apiFetch, problemDetail } from "@/lib/api";
import { filenameFrom, saveBlob } from "@/lib/download";

/**
 * Download one run's evaluated cohort as CSV or JSON (#102).
 *
 * Sits beside `ReportDownload` because the two are the same act for two readers:
 * the report is what an auditor reads, this is what a coordinator loads into a
 * CTMS or a spreadsheet. Placing them together is what makes that choice visible —
 * a coordinator who only ever saw "Download report" would export an HTML document
 * and then retype it.
 *
 * Two buttons rather than a dropdown. The formats are not variants of one action
 * to this reader: CSV is "get this into my spreadsheet" and JSON is "hand this to
 * an auditor", and at two options a menu costs a click to reveal what a pair of
 * labels says outright. It also keeps the control keyboard-reachable without a
 * popover.
 *
 * Rendered only for a run that has a cohort — see the call sites. The endpoint
 * itself answers a criteria-only run with an empty cohort rather than an error,
 * which is the right API behaviour and the wrong button to offer: "Export cohort"
 * on a run that has no patients promises a file worth opening.
 */
const FORMATS = [
  { format: "csv", label: "CSV", hint: "Cohort as CSV, for a spreadsheet or CTMS import" },
  { format: "json", label: "JSON", hint: "Cohort as JSON, with the approved criteria" },
] as const;

export function CohortExport({
  threadId,
  className,
  size = "sm",
  variant = "outline",
}: {
  threadId: string;
  className?: string;
  size?: "default" | "sm" | "lg";
  variant?: "default" | "outline" | "secondary";
}) {
  // The set of formats in flight, not a boolean and not a single slot. A boolean
  // would put both buttons into a spinner and hide which file is coming; a single
  // slot would let the first request's completion clear the spinner of a second
  // one still in flight, since the two downloads are independent and a reviewer
  // taking both files is a normal thing to do.
  const [busy, setBusy] = useState<ReadonlySet<string>>(new Set());
  const [error, setError] = useState<string | null>(null);

  function setFormatBusy(format: string, running: boolean) {
    setBusy((current) => {
      const next = new Set(current);
      if (running) next.add(format);
      else next.delete(format);
      return next;
    });
  }

  async function download(format: string) {
    setFormatBusy(format, true);
    setError(null);
    try {
      const response = await apiFetch(
        `/api/screenings/${encodeURIComponent(threadId)}/export?format=${format}`
      );
      if (!response.ok) {
        setError(await problemDetail(response, "Could not build the export"));
        return;
      }
      saveBlob(
        await response.blob(),
        filenameFrom(response.headers.get("content-disposition"), `trialgate-cohort.${format}`)
      );
    } catch {
      setError("Could not reach the server.");
    } finally {
      setFormatBusy(format, false);
    }
  }

  return (
    <div className={className} data-region="cohort-export">
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="text-muted-foreground text-xs">Export cohort</span>
        {FORMATS.map(({ format, label, hint }) => (
          <Button
            key={format}
            onClick={() => download(format)}
            // Only the pressed button is disabled: the other format is still a
            // legitimate second download, and two files is a normal thing to want.
            disabled={busy.has(format)}
            size={size}
            variant={variant}
            title={hint}
            aria-label={hint}
          >
            {busy.has(format) ? (
              <Loader2 className="animate-spin" aria-hidden="true" />
            ) : (
              <Sheet aria-hidden="true" />
            )}
            {label}
          </Button>
        ))}
      </div>
      {/* Inline for the same reason the report's is: the reason an export can't be
          built is about the run the reader is looking at. */}
      {error && (
        <p className="text-destructive pt-1.5 text-xs" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
