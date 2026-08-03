/**
 * Batch upload (#61): the submission's vocabulary, and the driver that walks the
 * created runs through the pipeline.
 *
 * `POST /api/screenings/batch` only *creates* the threads — like a single upload,
 * a screening runs when a client streams it. So the batch view has to drive N
 * streams, and this module owns that: the queue, the bounded concurrency, and the
 * translation from SSE frames into a phase a row can render. Kept out of the
 * component so the wire-level rules live beside the contract they follow rather
 * than inside JSX.
 */

import { apiFetch, problemDetail } from "@/lib/api";
import { statusLabel } from "@/lib/runs";
import { readEventStream } from "@/lib/sse";
import type { BatchItem, ScreeningStatus, StreamMessage } from "@/types";

/**
 * How many protocols one submission carries — mirrors `MAX_BATCH_FILES` in
 * backend/app/main.py, which is the enforcement. Checked client-side too so a
 * folder of thirty files is refused with a sentence about splitting it up, before
 * thirty documents are uploaded to earn the same answer.
 */
export const BATCH_MAX_FILES = 10;

/**
 * How many of a batch's runs stream at once.
 *
 * Two, against a server that allows four (`MAX_CONCURRENT_SCREENINGS`): a batch is
 * a background chore, and it must not be able to fill every slot on the instance
 * and 429 a colleague's single screening — or its own later rows. Raising this
 * would not make the batch finish much sooner anyway, since the pipeline's cost is
 * LLM calls that queue behind each other regardless.
 */
export const BATCH_CONCURRENCY = 2;

/**
 * A batch row's live phase. Deliberately the same vocabulary the runs index uses
 * (`ScreeningStatus`), so `statusLabel`/`statusVariant` render a row mid-flight
 * exactly as history will render it afterwards — plus `queued`, which is this
 * view's own state: created, not yet streaming.
 */
export type BatchPhase = ScreeningStatus | "queued";

export type BatchRun = {
  thread_id: string;
  filename: string;
  phase: BatchPhase;
  /** The API's own words when the run failed; null otherwise. */
  error: string | null;
};

/** Phases a batch row will not leave on its own — nothing more will stream. */
const TERMINAL_PHASES: ReadonlySet<BatchPhase> = new Set<BatchPhase>([
  "awaiting_approval",
  "done",
  "failed",
  "escalated",
]);

export function isSettled(phase: BatchPhase): boolean {
  return TERMINAL_PHASES.has(phase);
}

/**
 * How a phase reads on a row. Every phase but `queued` is a run status the rest of
 * the app already labels, so this only names the one that is ours.
 */
export function phaseLabel(phase: BatchPhase): string {
  return phase === "queued" ? "Queued" : statusLabel(phase);
}

/**
 * The frames that end a stream (backend/app/services/sse.py). Not the same thing
 * as a terminal *phase*: an escalated run reports `human_escalation` and then
 * still sends `__end__`, so the read has to continue to the sentinel while the
 * phase stays put.
 */
const TERMINAL_FRAMES: ReadonlySet<string> = new Set(["__interrupt__", "__end__", "__error__"]);

/** `{items}` from `POST /api/screenings/batch`, split into what ran and what didn't. */
export function splitBatchItems(items: BatchItem[]): {
  runs: BatchRun[];
  rejected: BatchItem[];
} {
  const runs: BatchRun[] = [];
  const rejected: BatchItem[] = [];
  for (const item of items) {
    if (item.thread_id) {
      runs.push({
        thread_id: item.thread_id,
        filename: item.filename,
        phase: "queued",
        error: null,
      });
    } else {
      rejected.push(item);
    }
  }
  return { runs, rejected };
}

/**
 * The phase a frame moves a row to, or null when the frame changes nothing.
 *
 * `stream_mode="updates"` emits when a node *finishes*, so a frame names the stage
 * that just stopped and the phase is the one after it — the same reading
 * `ScreeningRun.runningAgent` documents at length for the pipeline cards. Two
 * graph behaviours bend that: a Critic rejection sends the extraction back to the
 * parser, and `human_escalation` is terminal.
 */
export function phaseFromFrame(frame: StreamMessage): BatchPhase | null {
  switch (frame.node) {
    case "__interrupt__":
      return "awaiting_approval";
    case "__end__":
      return "done";
    case "__error__":
      return "failed";
    case "__progress__":
      return "matching";
    case "human_escalation":
      return "escalated";
    case "router":
      return "parsing";
    case "parser":
      return "critiquing";
    case "critic": {
      // A rejection is the Critic handing the extraction back for another parse;
      // anything else means it passed, and the run is heading for the gate (which
      // arrives as its own `__interrupt__` frame).
      const events = frame.update?.events ?? [];
      return events[events.length - 1]?.status === "rejected" ? "parsing" : "critiquing";
    }
    default:
      return null;
  }
}

/** Seconds to wait before retrying a 429, from the server's own Retry-After. */
function retryAfterSeconds(response: Response, fallback: number): number {
  const header = Number(response.headers.get("retry-after"));
  return Number.isFinite(header) && header > 0 ? header : fallback;
}

/** Wait, or resolve early if the view goes away mid-wait. */
function sleep(seconds: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, seconds * 1000);
    signal.addEventListener(
      "abort",
      () => {
        clearTimeout(timer);
        resolve();
      },
      { once: true }
    );
  });
}

// The concurrency gate is shared with every other client of the instance, so a
// batch row can find it full even though this view only ever holds
// BATCH_CONCURRENCY connections. Retry a bounded number of times on the server's
// own schedule rather than reporting a queueing run as a failed one.
const MAX_SATURATION_RETRIES = 5;
const SATURATION_FALLBACK_SECONDS = 5;

type Report = (threadId: string, phase: BatchPhase, error?: string | null) => void;

/**
 * Stream one run to its terminal frame, reporting each phase change.
 *
 * `fetch` rather than `EventSource` — the same reason the approve/edit streams use
 * it (lib/sse): a 429 from the saturated concurrency gate is a status code with a
 * Retry-After to honor, and EventSource would surface it as an anonymous error and
 * then reconnect on its own schedule.
 */
async function streamOneRun(run: BatchRun, signal: AbortSignal, report: Report): Promise<void> {
  const path = `/api/screenings/${encodeURIComponent(run.thread_id)}/stream`;
  for (let attempt = 0; attempt <= MAX_SATURATION_RETRIES; attempt++) {
    if (signal.aborted) return;
    report(run.thread_id, "routing");
    let response: Response;
    try {
      response = await apiFetch(path, { signal });
    } catch {
      // An abort lands here too; the page is leaving, so there is no row left to
      // report to.
      if (!signal.aborted) {
        report(run.thread_id, "failed", "Could not reach the server.");
      }
      return;
    }
    if (response.status === 429 && attempt < MAX_SATURATION_RETRIES) {
      report(run.thread_id, "queued");
      await sleep(retryAfterSeconds(response, SATURATION_FALLBACK_SECONDS), signal);
      continue;
    }
    // Including a 429 on the final attempt: the row then reports the gate's own
    // "too many screenings in progress" wording, which is the truth about why it
    // stopped, and the run is still there to be opened and streamed by hand.
    if (!response.ok) {
      report(run.thread_id, "failed", await problemDetail(response, "Screening failed"));
      return;
    }
    let phase: BatchPhase = "routing";
    try {
      await readEventStream(response, (frame) => {
        const next = phaseFromFrame(frame);
        // `__end__` closes an escalated run too, and "done" is the wrong word for
        // it: the phase a settled row already holds wins over the sentinel's.
        if (next && !(frame.node === "__end__" && isSettled(phase))) {
          phase = next;
          report(run.thread_id, next, frame.node === "__error__" ? frame.message : null);
        }
        return TERMINAL_FRAMES.has(frame.node);
      });
    } catch {
      if (!signal.aborted && !isSettled(phase)) {
        report(run.thread_id, "failed", "The screening stream ended unexpectedly.");
      }
      return;
    }
    // A stream that ends without a terminal frame is a dropped connection, not a
    // finished run — say so rather than leaving the row spinning forever.
    if (!signal.aborted && !isSettled(phase)) {
      report(run.thread_id, "failed", "Connection to the screening stream failed.");
    }
    return;
  }
}

/**
 * Run every created screening, `BATCH_CONCURRENCY` at a time, reporting phases as
 * they change. Resolves when the batch has settled (or the signal aborts).
 *
 * A worker pool over a shared cursor rather than fixed slices: protocols differ
 * wildly in length, so a slice-per-worker would leave one worker idle while
 * another still had three long documents to get through.
 */
export async function runBatch(
  runs: BatchRun[],
  signal: AbortSignal,
  report: Report
): Promise<void> {
  let cursor = 0;
  const workers = Array.from({ length: Math.min(BATCH_CONCURRENCY, runs.length) }, async () => {
    while (!signal.aborted) {
      const index = cursor++;
      if (index >= runs.length) return;
      await streamOneRun(runs[index], signal, report);
    }
  });
  await Promise.all(workers);
}
