"use client";

import { useState } from "react";
import { Download, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { apiFetch, problemDetail } from "@/lib/api";

/**
 * Download one run's screening report (#56).
 *
 * Fetched rather than linked. A plain `<a href download>` would work — the report
 * route is same-origin and the session is a cookie the browser attaches itself —
 * but it has no error channel: a 401 on an expired session, a 409 for a run that
 * never streamed, or a 404 would all navigate the user to a JSON error body, and
 * `apiFetch`'s session-expiry handler would never fire. Going through fetch keeps
 * both (the message renders inline, the expiry redirects) at the cost of holding
 * the document in memory for the length of one click.
 *
 * The filename comes from the server's `Content-Disposition` so the file a
 * reviewer ends up with is named by the same rule everywhere; the fallback covers
 * a topology where the header isn't readable (a cross-origin dev proxy that
 * doesn't expose it), where a generic name still beats a blob id.
 */
function filenameFrom(disposition: string | null): string {
  const match = disposition?.match(/filename="([^"]+)"/);
  return match?.[1] ?? "trialgate-report.html";
}

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
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      // A synthetic click is the only way to hand a fetched body to the browser's
      // download machinery. Two details are not stylistic: the anchor has to be
      // *in the document* for a programmatic click to start a download in Firefox,
      // and the object URL must be revoked on a later task rather than on the line
      // after `click()` — revoking inside the same task cancels the download in
      // some browsers, while never revoking pins the whole document in memory for
      // the lifetime of the page.
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = filenameFrom(response.headers.get("content-disposition"));
      anchor.hidden = true;
      document.body.append(anchor);
      anchor.click();
      anchor.remove();
      setTimeout(() => URL.revokeObjectURL(url), 0);
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
