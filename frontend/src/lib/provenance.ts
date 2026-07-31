/**
 * Turning resolved spans into something renderable (#54).
 *
 * The backend answers `GET /api/screenings/{id}/protocol` with the protocol text
 * and a `[start, end)` span for each criterion's `source_text` it could locate
 * (app/services/provenance.py owns the matching — it is fuzzy enough to deserve
 * tests, and both halves of the input are already on that side). This module is
 * the small part that stays here: indexing those spans by sentence, and cutting
 * the text into the alternating plain/highlighted pieces the viewer maps over.
 */

import type { SourceSpan } from "@/types";

/** One run of protocol text: `source` is null for the stretches between spans. */
export type ProtocolSegment = {
  text: string;
  /** The `source_text` this run belongs to — the key the selection compares on. */
  source: string | null;
  /** False when the span is a partial match; meaningless for a plain run. */
  exact: boolean;
};

/**
 * The protocol cut into runs, with every locatable passage marked.
 *
 * Overlapping spans are dropped rather than nested: one sentence can be the
 * head of another (a partial match against a longer criterion), and a `<mark>`
 * inside a `<mark>` would render as a darker patch that belongs to neither. The
 * earlier span wins — spans arrive in extraction order, so that is the criterion
 * a reader meets first in the table.
 *
 * Every character of `text` appears in exactly one segment, so the rendered
 * document is the uploaded one and not a lossy reconstruction of it.
 */
export function segmentProtocol(text: string, spans: SourceSpan[]): ProtocolSegment[] {
  const ordered = [...spans]
    .filter((span) => span.start < span.end && span.start >= 0 && span.end <= text.length)
    .sort((a, b) => a.start - b.start || b.end - a.end);

  const segments: ProtocolSegment[] = [];
  let cursor = 0;
  for (const span of ordered) {
    if (span.start < cursor) continue; // overlaps one already emitted
    if (span.start > cursor) {
      segments.push({ text: text.slice(cursor, span.start), source: null, exact: true });
    }
    segments.push({
      text: text.slice(span.start, span.end),
      source: span.source_text,
      exact: span.exact,
    });
    cursor = span.end;
  }
  if (cursor < text.length) {
    segments.push({ text: text.slice(cursor), source: null, exact: true });
  }
  return segments;
}
