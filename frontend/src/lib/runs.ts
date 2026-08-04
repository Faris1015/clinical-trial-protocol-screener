/**
 * Shared vocabulary for the runs history (#51) — the index and the detail view
 * both need to render a status and link between each other, so the mapping and
 * the URL shape live here rather than being duplicated (and drifting) in two
 * pages.
 */

import type { ScreeningStatus } from "@/types";

/**
 * Deep link to one run's detail view.
 *
 * A query parameter rather than the `/runs/<threadId>` path segment the issue
 * sketches: the frontend is a static export (`output: "export"`), so a dynamic
 * segment would need `generateStaticParams` to enumerate every thread id at
 * build time — impossible for ids minted after the build, and an unexported
 * path is a hard 404 on both static hosts. `/runs/view/` is one exported page
 * that reads its id at runtime, so the link is still bookmarkable and
 * shareable. next.config.ts documents the alternative (dropping the static
 * export for a Next server) and what it would cost.
 *
 * The trailing slash on the route is load-bearing: `trailingSlash: true` means
 * the export writes `runs/view/index.html`, and the client router looks for
 * `runs/view.txt` — a 404 and a full page reload — if the slash is missing.
 */
export function runHref(threadId: string): string {
  return `/runs/view/?id=${encodeURIComponent(threadId)}`;
}

/**
 * Deep link to the side-by-side comparison of two runs (#59).
 *
 * Both ids in the query string, matching the API's own `?a=&b=` shape, and a
 * query parameter for the same static-export reason `runHref` uses one. Order is
 * load-bearing: `a` is the left column and additions/removals are stated from its
 * point of view, so swapping the pair mirrors the diff.
 */
export function compareHref(a: string, b: string): string {
  return `/runs/compare/?a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}`;
}

/** Every status, in pipeline order — the order the filter offers them in. */
export const RUN_STATUSES: ScreeningStatus[] = [
  "routing",
  "parsing",
  "critiquing",
  "awaiting_approval",
  "matching",
  "done",
  "failed",
  "escalated",
];

const STATUS_LABELS: Record<ScreeningStatus, string> = {
  routing: "Routing",
  parsing: "Parsing",
  critiquing: "Critiquing",
  awaiting_approval: "Awaiting approval",
  matching: "Matching",
  done: "Done",
  failed: "Failed",
  escalated: "Escalated",
};

/**
 * A status the API is free to add later still has to render as something, so
 * this falls back to the raw value rather than a blank cell.
 */
export function statusLabel(status: string): string {
  return STATUS_LABELS[status as ScreeningStatus] ?? status;
}

/**
 * Which badge colour a status reads as, reusing the clinical status tokens:
 * `done` is the only success, `failed` is the only outright failure, and
 * everything else is either in flight or waiting on a human — neither of which
 * is an error, so they stay neutral rather than shouting.
 */
export function statusVariant(status: string): "pass" | "fail" | "warn" | "secondary" {
  switch (status) {
    case "done":
      return "pass";
    case "failed":
      return "fail";
    case "awaiting_approval":
    case "escalated":
      return "warn";
    default:
      return "secondary";
  }
}

/**
 * An ISO-8601 timestamp as the viewer's local date and time.
 *
 * Rendered client-side only (the pages fetch after mount), so there is no
 * server/client locale mismatch to hydrate around. A malformed value renders as
 * itself instead of "Invalid Date".
 */
export function formatTimestamp(iso: string): string {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return iso;
  return at.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

/**
 * The wall-clock time alone, to the second — for a list of instants that all
 * share a date (#55's timeline). Repeating "Jul 31, 2026" on every step of a run
 * that took nine milliseconds says nothing; the seconds do. The date is stated
 * once, above the list.
 */
export function formatTime(iso: string): string {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return iso;
  return at.toLocaleTimeString(undefined, { timeStyle: "medium" });
}
