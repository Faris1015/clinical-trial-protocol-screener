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

  async function upload(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    const body = new FormData();
    body.append("file", file);
    const res = await fetch("/api/screenings", { method: "POST", body });
    const { thread_id } = (await res.json()) as { thread_id: string };
    setThreadId(thread_id);
  }

  return (
    <>
      <label className="upload">
        Upload protocol (PDF or .md)
        <input type="file" accept=".pdf,.md,.txt" onChange={upload} />
      </label>

      {/* Keyed by thread: a second upload mounts a fresh run rather than mixing
          the new stream's frames into the previous screening's state. */}
      <ScreeningRun key={threadId ?? "idle"} threadId={threadId} />
    </>
  );
}
