"use client";

import { useState } from "react";
import { Download, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { apiFetch, problemDetail } from "@/lib/api";
import { filenameFrom, saveBlob } from "@/lib/download";

/**
 * Download one run's screening report (#56).
 *
 * The document a reviewer hands off. Its machine-readable sibling is
 * `cohort-export.tsx` (#102), which sits beside it and shares the fetch-and-save
 * mechanics in `lib/download.ts` — see there for why both are fetched rather than
 * linked.
 */
export function ReportDownload({
  threadId,
  className,
  size = "lg",
  variant = "outline",
}: {
  threadId: string;
  className?: string;
  size?: "default" | "sm" | "lg";
  variant?: "default" | "outline" | "secondary";
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function download() {
    setBusy(true);
    setError(null);
    try {
      const response = await apiFetch(`/api/screenings/${encodeURIComponent(threadId)}/report`);
      if (!response.ok) {
        setError(await problemDetail(response, "Could not build the report"));
        return;
      }
      saveBlob(
        await response.blob(),
        filenameFrom(response.headers.get("content-disposition"), "trialgate-report.html")
      );
    } catch {
      setError("Could not reach the server.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className={className} data-region="report-download">
      <Button onClick={download} disabled={busy} size={size} variant={variant}>
        {busy ? (
          <Loader2 className="animate-spin" aria-hidden="true" />
        ) : (
          <Download aria-hidden="true" />
        )}
        {busy ? "Preparing report…" : "Download report"}
      </Button>
      {/* Inline rather than a toast: the reason a report can't be built is about
          the run the reader is looking at (it never streamed, the session
          expired), so it belongs beside the button they pressed. */}
      {error && (
        <p className="text-destructive pt-1.5 text-xs" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
