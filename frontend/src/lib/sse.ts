/**
 * Reading an SSE stream that arrived as a `fetch` response body.
 *
 * `EventSource` handles the initial `GET /stream`, but it can only ever issue a
 * GET — so the two routes that resume a run (`POST /approve` and, since #53,
 * `PATCH /criteria`) hand back a `text/event-stream` body that has to be framed
 * by hand. Both do it identically, and both feed the frames into the same reducer
 * (`useScreenerStream.applyFrame`), so the wire-level parsing lives here once
 * rather than being copy-pasted per caller.
 *
 * Mirrors the framing in `backend/app/services/sse.py`: frames are separated by a
 * blank line, payloads arrive on a `data:` line, and a leading-colon comment line
 * (`: heartbeat`) is keepalive to be skipped rather than parsed.
 */

import type { StreamMessage } from "@/types";

/**
 * Read `response` to completion, invoking `onMessage` for each data frame.
 *
 * Stops early — releasing the reader, which closes the connection and lets the
 * server free the run's concurrency slot — as soon as `onMessage` returns true
 * for a terminal frame. Without that, a caller would keep the socket open past
 * the end of the run waiting for bytes that never come.
 */
export async function readEventStream(
  response: Response,
  onMessage: (message: StreamMessage) => boolean | void
): Promise<void> {
  if (!response.body) return;
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let separator: number;
      while ((separator = buffer.indexOf("\n\n")) !== -1) {
        const block = buffer.slice(0, separator);
        buffer = buffer.slice(separator + 2);
        const dataLine = block.split("\n").find((line) => line.startsWith("data:"));
        if (!dataLine) continue;
        const message = JSON.parse(dataLine.slice("data:".length).trim()) as StreamMessage;
        if (onMessage(message) === true) return;
      }
    }
  } finally {
    // Runs on the early return and on a throw from `onMessage` alike, so a
    // half-read stream never leaves the connection dangling.
    reader.cancel().catch(() => {});
  }
}
