"use client";

import { useEffect, useState } from "react";
import { CriteriaTable } from "@/components/CriteriaTable";
import { ProtocolView } from "@/components/provenance/protocol-view";
import { apiFetch, problemDetail } from "@/lib/api";
import type { CriteriaSchema, ProtocolPayload } from "@/types";

/**
 * The criteria and the protocol they were read out of, side by side (#54).
 *
 * Owns the one piece of state the two halves share — which criterion is selected
 * — so the table and the document can't disagree about it, and it is the only
 * component that knows the protocol endpoint exists. Both the live screening and
 * the replay of a past run mount this in place of a bare `CriteriaTable`, under
 * `key={threadId}` so switching runs yields a clean instance.
 *
 * The protocol is fetched once per run rather than streamed with the criteria:
 * it is the *upload*, fixed from the moment the screening was created, while the
 * criteria are revised by the Critic loop and by reviewer edits (#53). Re-reading
 * it on every parse would refetch an unchanged document.
 */
export function CriteriaProvenance({
  threadId,
  criteria,
}: {
  threadId: string | null;
  criteria: CriteriaSchema | null;
}) {
  const [protocol, setProtocol] = useState<ProtocolPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);

  // No reset of the three states above when `threadId` changes: callers mount
  // this under `key={threadId}`, exactly as they do ScreeningRun, so a different
  // run gets a clean instance rather than a hand-rolled teardown that has to
  // remember every piece of state.
  useEffect(() => {
    if (!threadId) return;
    let active = true;
    apiFetch(`/api/screenings/${encodeURIComponent(threadId)}/protocol`)
      .then(async (response) => {
        if (!active) return;
        if (!response.ok) {
          setError(await problemDetail(response, "Could not load the protocol text"));
          return;
        }
        setProtocol((await response.json()) as ProtocolPayload);
      })
      .catch(() => {
        if (active) setError("Could not reach the server.");
      });
    return () => {
      active = false;
    };
  }, [threadId]);

  if (!criteria) return null;

  // No thread means no document to point into (the live page before its first
  // upload), so the chips stay inert rather than selecting into nothing.
  if (!threadId) return <CriteriaTable criteria={criteria} />;

  return (
    // Stacked on narrow screens, where two columns would leave the protocol
    // pane too cramped to read a wrapped sentence in. `items-start` so the
    // shorter card doesn't stretch to the taller one's height.
    <div className="grid items-start gap-3 lg:grid-cols-2" data-region="criteria-provenance">
      <CriteriaTable criteria={criteria} selectedSource={selected} onSelectSource={setSelected} />
      <ProtocolView
        text={protocol?.text ?? ""}
        spans={protocol?.spans ?? []}
        filename={protocol?.source_filename}
        selected={selected}
        loading={!protocol && !error}
        error={error}
      />
    </div>
  );
}
