"use client";

import { useState } from "react";
import { ScreeningRun } from "@/components/ScreeningRun";

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
      res = await fetch("/api/screenings", { method: "POST", body });
    } catch {
      setUploadError("Could not reach the server. Check your connection and try again.");
      return;
    }
    if (!res.ok) {
      // Every rejection the API knows how to describe (413 too large, 415 wrong
      // type, 422 unreadable document, 429 rate limited, 503 backend down)
      // arrives as {error, detail}. Surface it and leave any screening already on
      // screen untouched — a failed upload started nothing, so it should not look
      // like it wiped the previous run.
      const problem = (await res.json().catch(() => ({}))) as { detail?: string };
      setUploadError(problem.detail ?? `Upload failed (${res.status})`);
      return;
    }
    const { thread_id } = (await res.json()) as { thread_id: string };
    setThreadId(thread_id);
  }

  return (
    <>
      <label className="upload">
        Upload protocol (PDF or .md)
        <input type="file" accept=".pdf,.md,.txt" onChange={upload} />
      </label>

      {uploadError && <div className="banner failed">{uploadError}</div>}

      {/* Keyed by thread: a second upload mounts a fresh run rather than mixing
          the new stream's frames into the previous screening's state. */}
      <ScreeningRun key={threadId ?? "idle"} threadId={threadId} />
    </>
  );
}
