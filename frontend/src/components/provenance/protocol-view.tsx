"use client";

import { useEffect, useMemo, useRef } from "react";
import { AlertTriangle } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { segmentProtocol } from "@/lib/provenance";
import type { SourceSpan } from "@/types";

/**
 * The uploaded protocol, with the selected criterion's source passage marked (#54).
 *
 * The text is rendered as DOM text nodes inside a scroll pane — never as markup,
 * whatever an uploaded document contains — and every passage the backend could
 * locate is faintly underlined, so the page shows at a glance how much of the
 * protocol the extraction actually accounts for. Selecting a criterion lifts one
 * of those to a full highlight and brings it into view.
 */
export function ProtocolView({
  text,
  spans,
  selected,
  loading = false,
  error = null,
  filename,
}: {
  text: string;
  spans: SourceSpan[];
  /** The selected criterion's `source_text`, or null when nothing is selected. */
  selected: string | null;
  loading?: boolean;
  error?: string | null;
  filename?: string;
}) {
  const pane = useRef<HTMLDivElement>(null);
  const active = useRef<HTMLElement>(null);

  // Scroll the pane, not the page: `scrollIntoView` walks every scrollable
  // ancestor, which would yank a reader away from the criteria chip they just
  // clicked. The pane is `relative`, so a mark's offsetParent is the pane and
  // `offsetTop` is already the coordinate we want.
  useEffect(() => {
    const container = pane.current;
    const mark = active.current;
    if (!container || !mark || !selected) return;
    const target = mark.offsetTop - container.clientHeight / 2 + mark.offsetHeight / 2;
    container.scrollTo({ top: Math.max(0, target), behavior: "smooth" });
  }, [selected]);

  const located = spans.find((span) => span.source_text === selected);
  // Selecting a criterion re-renders this component but changes nothing about
  // how the document is cut up, and the document runs to a couple of hundred
  // thousand characters.
  const segments = useMemo(() => segmentProtocol(text, spans), [text, spans]);
  // The first run of the selected passage carries the scroll target; a sentence
  // is one span, so this is only defensive against a future many-to-one change.
  const anchorIndex = segments.findIndex((segment) => segment.source === selected);

  return (
    <Card data-region="protocol-text" className="gap-0">
      <CardHeader>
        <CardTitle className="text-base">Protocol text</CardTitle>
        {filename && <p className="text-muted-foreground truncate font-mono text-xs">{filename}</p>}
      </CardHeader>
      <CardContent className="space-y-3">
        {/* Said before the pane rather than inside it: when the sentence isn't
            there, scrolling the pane tells the reader nothing, so the answer has
            to be somewhere they are already looking. */}
        {selected && !loading && !error && !located && (
          <p
            className="text-status-warn flex items-start gap-2 text-xs"
            role="status"
            data-region="source-not-found"
          >
            <AlertTriangle className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />
            <span>
              This criterion&apos;s source sentence could not be found in the protocol text — the
              extraction may have reworded it. The sentence it claims is: “{selected}”
            </span>
          </p>
        )}
        {located && !located.exact && (
          <p className="text-muted-foreground text-xs" role="status">
            Closest match only — the highlighted passage is where this criterion’s wording starts
            diverging from the protocol.
          </p>
        )}

        {loading && <Skeleton className="h-64 w-full" aria-hidden="true" />}

        {error && (
          <p className="text-status-warn flex items-start gap-2 text-sm" role="alert">
            <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
            <span>{error}</span>
          </p>
        )}

        {!loading && !error && (
          <div
            ref={pane}
            // Focusable so the pane can be scrolled from the keyboard; a plain
            // overflow container is unreachable without a pointer.
            tabIndex={0}
            role="region"
            aria-label="Protocol text"
            className="border-border bg-muted/30 relative max-h-128 overflow-y-auto rounded-md border p-3 font-mono text-xs leading-relaxed whitespace-pre-wrap"
          >
            {segments.map((segment, i) => {
              if (!segment.source) return <span key={i}>{segment.text}</span>;
              const isSelected = segment.source === selected;
              return (
                <mark
                  key={i}
                  ref={i === anchorIndex ? active : undefined}
                  data-selected={isSelected || undefined}
                  className={
                    isSelected
                      ? "bg-status-warn-soft text-foreground ring-status-warn/50 rounded-sm ring-1"
                      : "decoration-border bg-transparent text-inherit underline decoration-2 underline-offset-4"
                  }
                >
                  {segment.text}
                </mark>
              );
            })}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
