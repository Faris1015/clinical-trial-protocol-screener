"use client";

import { useState } from "react";
import { AlertTriangle, Upload } from "lucide-react";
import { BatchScreening } from "@/components/batch/batch-screening";
import { ScreeningRun } from "@/components/ScreeningRun";
import { PageHeader } from "@/components/shell/page-header";
import { Card, CardContent } from "@/components/ui/card";
import { apiFetch, problemDetail } from "@/lib/api";
import { BATCH_MAX_FILES } from "@/lib/batch";
import type { BatchCreated } from "@/types";

/**
 * New-screening route: upload one protocol and watch it run, or several and watch
 * the batch (#61). A client component end to end — every byte of state below comes
 * from a live SSE connection, so there is nothing for the server to render ahead of
 * time.
 *
 * One file and many files are two views rather than one generalized view: a single
 * screening shows its pipeline, criteria, provenance and approval gate in full,
 * and that is exactly what does not scale to ten of them side by side. The upload
 * control is shared, so the reviewer makes no choice up front — the number of
 * files they pick decides which view they get.
 */
export default function NewScreeningPage() {
  const [threadId, setThreadId] = useState<string | null>(null);
  const [batch, setBatch] = useState<BatchCreated | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);
  // Bumped per submission and used as the batch view's key, so re-uploading the
  // same set of files mounts a fresh batch instead of leaving the finished one on
  // screen (the previous response object would otherwise be shallow-equal enough
  // to keep its rows).
  const [submission, setSubmission] = useState(0);

  async function upload(e: React.ChangeEvent<HTMLInputElement>) {
    const files = Array.from(e.target.files ?? []);
    if (files.length === 0) return;
    // Clear the input now that the files are captured. A file input only fires
    // `change` when its value actually changes, so without this, re-selecting the
    // same protocol — the obvious way to retry a failed upload — is a silent
    // no-op.
    e.target.value = "";
    setUploadError(null);

    // Refused here as well as by the API: uploading thirty documents only to be
    // told the limit is ten wastes the upload and the reviewer's time.
    if (files.length > BATCH_MAX_FILES) {
      setUploadError(
        `Select at most ${BATCH_MAX_FILES} protocols at once — this batch has ${files.length}. ` +
          "Split it into smaller batches."
      );
      return;
    }

    const single = files.length === 1;
    const body = new FormData();
    // Two endpoints, one field name each: `file` for a single screening, repeated
    // `files` for a batch. A one-file batch would work, but it would trade the live
    // pipeline view for a one-row table, which is a worse answer to "screen this
    // protocol".
    for (const file of files) body.append(single ? "file" : "files", file);

    let res: Response;
    try {
      res = await apiFetch(single ? "/api/screenings" : "/api/screenings/batch", {
        method: "POST",
        body,
      });
    } catch {
      setUploadError("Could not reach the server. Check your connection and try again.");
      return;
    }
    if (!res.ok) {
      // Every rejection the API knows how to describe (401 expired session, 413
      // too large, 415 wrong type, 422 unreadable document or too many files, 429
      // rate limited, 503 backend down) arrives as {error, detail}. Surface it and
      // leave any screening already on screen untouched — a failed upload started
      // nothing, so it should not look like it wiped the previous run.
      setUploadError(await problemDetail(res, "Upload failed"));
      return;
    }

    setSubmission((n) => n + 1);
    if (single) {
      const { thread_id } = (await res.json()) as { thread_id: string };
      setBatch(null);
      setThreadId(thread_id);
      return;
    }
    // A batch answers 200 even when some files were refused — the accepted ones
    // are already screening. The view lists both, so the error card above stays
    // for whole-submission failures only.
    setThreadId(null);
    setBatch((await res.json()) as BatchCreated);
  }

  return (
    <>
      <PageHeader
        title="New Screening"
        description="Upload one or more trial protocols to parse their eligibility criteria and match a cohort."
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
          <span className="text-sm font-medium">Upload protocols</span>
          <span className="text-muted-foreground text-xs">
            PDF, Markdown or plain text · up to {BATCH_MAX_FILES} at once
          </span>
          <input
            type="file"
            accept=".pdf,.md,.txt"
            multiple
            onChange={upload}
            className="sr-only"
          />
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

        {/* Keyed by submission: a second upload mounts a fresh view rather than
            mixing the new stream's frames into the previous screening's state. The
            idle pipeline is what an unused page shows, so the single-run view holds
            the floor until a batch replaces it. */}
        {batch ? (
          <BatchScreening key={`batch-${submission}`} created={batch} />
        ) : (
          <ScreeningRun key={threadId ?? "idle"} threadId={threadId} />
        )}
      </div>
    </>
  );
}
