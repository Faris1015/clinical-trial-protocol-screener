"use client";

import { useState } from "react";
import { Loader2, Sheet } from "lucide-react";
import { Button } from "@/components/ui/button";
import { apiFetch, problemDetail } from "@/lib/api";
import { filenameFrom, saveBlob } from "@/lib/download";

/**
 * Download the audit log as CSV or JSON (#98, AC 6).
 *
 * `CohortExport`'s shape, deliberately — two buttons rather than a dropdown, the
 * same fetch-then-save so a 401 or a 422 renders inline instead of navigating the
 * reader to a JSON error body. The two readers are the same two: CSV is "get this
 * into my spreadsheet", JSON is "hand this to an external auditor".
 *
 * `query` is the index's own query string, minus its paging. That is the whole
 * point of taking it: the file an auditor downloads is the view they were looking
 * at, not a second and wider query — and the server applies the same role scope to
 * both, so a reviewer cannot export what they cannot read.
 */
const FORMATS = [
  { format: "csv", label: "CSV", hint: "Audit log as CSV, for a spreadsheet" },
  { format: "json", label: "JSON", hint: "Audit log as JSON, for an external auditor" },
] as const;

export function AuditExport({ query }: { query: string }) {
  // The set of formats in flight rather than a boolean: the two downloads are
  // independent, and taking both files is a normal thing to do.
  const [busy, setBusy] = useState<ReadonlySet<string>>(new Set());
  const [error, setError] = useState<string | null>(null);

  function setFormatBusy(format: string, running: boolean) {
    setBusy((current) => {
      const next = new Set(current);
      if (running) next.add(format);
      else next.delete(format);
      return next;
    });
  }

  async function download(format: string) {
    setFormatBusy(format, true);
    setError(null);
    try {
      // `limit`/`offset` are the table's paging and mean nothing to a download —
      // the export carries every row the filter matched, up to its own cap.
      const params = new URLSearchParams(query);
      params.delete("limit");
      params.delete("offset");
      params.set("format", format);
      const response = await apiFetch(`/api/audit/export?${params.toString()}`);
      if (!response.ok) {
        setError(await problemDetail(response, "Could not build the export"));
        return;
      }
      saveBlob(
        await response.blob(),
        filenameFrom(response.headers.get("content-disposition"), `trialgate-audit.${format}`)
      );
    } catch {
      setError("Could not reach the server.");
    } finally {
      setFormatBusy(format, false);
    }
  }

  return (
    <div data-region="audit-export">
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="text-muted-foreground text-xs">Export</span>
        {FORMATS.map(({ format, label, hint }) => (
          <Button
            key={format}
            onClick={() => download(format)}
            disabled={busy.has(format)}
            size="sm"
            variant="outline"
            title={hint}
            aria-label={hint}
          >
            {busy.has(format) ? (
              <Loader2 className="animate-spin" aria-hidden="true" />
            ) : (
              <Sheet aria-hidden="true" />
            )}
            {label}
          </Button>
        ))}
      </div>
      {error && (
        <p className="text-destructive pt-1.5 text-xs" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
