"use client";

import { useState } from "react";
import { AlertTriangle, Ban } from "lucide-react";
import { Button } from "@/components/ui/button";
import { apiFetch, problemDetail } from "@/lib/api";
import { FIELD } from "@/lib/field";

/** Mirrors `MAX_REJECTION_REASON_CHARS` in backend/app/main.py. */
const MAX_REASON_CHARS = 2000;

/**
 * The gate's other answer (#91): stop this screening, and say why.
 *
 * One component for both places a reviewer can reach the decision — the live
 * screening's gate banner and the criteria editor they land on from the review
 * queue — because the decision is the same one and a second copy of it would be a
 * second chance to word the confirmation differently.
 *
 * Two-step by construction. The button alone does nothing: it opens the reason
 * field, and only a non-empty reason enables the confirm. Rejection is terminal
 * and cannot be undone from the app (a rejected run is no longer editable), so a
 * single mis-click must not be able to end a run — and the reason the API insists
 * on is the same thing that makes an accidental confirm impossible.
 *
 * `onRejected` fires only after the API accepts, for the same reason the approve
 * path sets `approvedBy` after its own response lands: a screen claiming a run was
 * rejected when the server refused (409, expired session) is worse than no
 * feedback at all.
 */
export function RejectScreening({
  threadId,
  disabled = false,
  onRejected,
}: {
  threadId: string;
  disabled?: boolean;
  onRejected?: (reason: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const trimmed = reason.trim();

  async function submit() {
    if (!trimmed || busy) return;
    setBusy(true);
    setError(null);
    try {
      const response = await apiFetch(`/api/screenings/${encodeURIComponent(threadId)}/reject`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reason: trimmed }),
      });
      if (!response.ok) {
        // 404 unknown thread, 409 a run that isn't at a decision point, 422 an
        // empty or oversized reason, 401 an expired session. The run is untouched
        // in all of them, so the typed reason stays on screen.
        setError(await problemDetail(response, "Could not reject this screening"));
        return;
      }
      setOpen(false);
      onRejected?.(trimmed);
    } catch {
      setError("Could not reach the server.");
    } finally {
      setBusy(false);
    }
  }

  if (!open) {
    return (
      <Button
        variant="destructive"
        className="shrink-0"
        disabled={disabled}
        onClick={() => setOpen(true)}
        data-region="reject-open"
      >
        <Ban aria-hidden="true" />
        Reject screening
      </Button>
    );
  }

  // A bordered block rather than a Card: this is rendered *inside* the gate
  // banner and the editor's action card, and a card nested in a card reads as a
  // second, unrelated thing on the page. `w-full` so it takes its own line when
  // the container it drops into is a wrapping row of buttons.
  return (
    <div
      className="border-destructive/40 bg-destructive/10 w-full space-y-3 rounded-lg border p-3 text-sm"
      data-region="reject-form"
    >
      <label className="block space-y-1.5" htmlFor="reject-reason">
        <span className="font-medium">Why is this protocol not screenable?</span>
        <span className="text-muted-foreground block text-xs">
          Recorded against the run permanently, with your name — this is what whoever reads the
          screening later will see instead of a cohort.
        </span>
        <textarea
          id="reject-reason"
          className={`${FIELD} h-24 w-full resize-y`}
          maxLength={MAX_REASON_CHARS}
          value={reason}
          disabled={busy}
          autoFocus
          onChange={(e) => setReason(e.target.value)}
        />
      </label>

      {error && (
        <p className="text-destructive flex items-start gap-2" role="alert">
          <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
          <span>{error}</span>
        </p>
      )}

      <div className="flex flex-wrap justify-end gap-2">
        <Button
          variant="outline"
          disabled={busy}
          onClick={() => {
            setOpen(false);
            setError(null);
          }}
        >
          Cancel
        </Button>
        {/* Disabled until there is a reason: the API requires one, and telling
              the reviewer here beats a 422 that costs a round trip and a
              rate-limit slot. */}
        <Button variant="destructive" disabled={busy || !trimmed} onClick={submit}>
          <Ban aria-hidden="true" />
          {busy ? "Rejecting…" : "Confirm rejection"}
        </Button>
      </div>
    </div>
  );
}
