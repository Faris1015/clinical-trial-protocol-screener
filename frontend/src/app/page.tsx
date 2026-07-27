"use client";

import { useState } from "react";
import { AlertTriangle, Upload } from "lucide-react";
import { ScreeningRun } from "@/components/ScreeningRun";
import { PageHeader } from "@/components/shell/page-header";
import { Card, CardContent } from "@/components/ui/card";
import { apiFetch, problemDetail } from "@/lib/api";

/**
 * New-screening route: upload a protocol, then watch that screening run. A client
 * component end to end — every byte of state below comes from a live SSE
 * connection, so there is nothing for the server to render ahead of time.
 */
export default function NewScreeningPage() {
  const [threadId, setThreadId] = useState<string | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);

  async function upload(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    // Clear the input now that the file is captured. A file input only fires
    // `change` when its value actually changes, so without this, re-selecting the
    // same protocol — the obvious way to retry a failed upload — is a silent
    // no-op.
    e.target.value = "";
    setUploadError(null);

    const body = new FormData();
    body.append("file", file);
    let res: Response;
    try {
      res = await apiFetch("/api/screenings", { method: "POST", body });
    } catch {
      setUploadError("Could not reach the server. Check your connection and try again.");
      return;
    }
    if (!res.ok) {
      // Every rejection the API knows how to describe (401 expired session, 413
      // too large, 415 wrong type, 422 unreadable document, 429 rate limited, 503
      // backend down) arrives as {error, detail}. Surface it and leave any
      // screening already on screen untouched — a failed upload started nothing,
      // so it should not look like it wiped the previous run.
      setUploadError(await problemDetail(res, "Upload failed"));
      return;
    }
    const { thread_id } = (await res.json()) as { thread_id: string };
    setThreadId(thread_id);
  }

  return (
    <>
      <PageHeader
        title="New Screening"
        description="Upload a trial protocol to parse its eligibility criteria and match a cohort."
      />

      <div className="space-y-4">
        {/* A <label> wrapping a real file input, rather than a Button that proxies
            a click at the input: the whole dropzone is the label, so it stays the
            native control — keyboard activation, the OS picker, and screen-reader
            semantics all come for free, and no browser lets a picker be opened
            without a user gesture anyway.

            The input is sr-only rather than hidden: `display:none` would take it
            out of the tab order, whereas this keeps it focusable, and
            focus-within paints the ring on the dropzone so that focus is
            visible. */}
        <label
          className="border-border bg-card hover:border-primary/60 hover:bg-accent/40 focus-within:border-primary focus-within:ring-ring/50 flex cursor-pointer flex-col items-center gap-2 rounded-lg border border-dashed px-6 py-8 text-center transition-colors focus-within:ring-3"
          data-region="upload"
        >
          <Upload className="text-muted-foreground size-5" aria-hidden="true" />
          <span className="text-sm font-medium">Upload protocol</span>
          <span className="text-muted-foreground text-xs">PDF, Markdown or plain text</span>
          <input type="file" accept=".pdf,.md,.txt" onChange={upload} className="sr-only" />
        </label>

        {uploadError && (
          <Card
            data-region="upload-error"
            role="alert"
            className="border-destructive/40 bg-destructive/10"
          >
            <CardContent className="flex items-start gap-2.5 text-sm">
              <AlertTriangle
                className="text-destructive mt-0.5 size-4 shrink-0"
                aria-hidden="true"
              />
              <span>{uploadError}</span>
            </CardContent>
          </Card>
        )}

        {/* Keyed by thread: a second upload mounts a fresh run rather than mixing
            the new stream's frames into the previous screening's state. */}
        <ScreeningRun key={threadId ?? "idle"} threadId={threadId} />
      </div>
    </>
  );
}
