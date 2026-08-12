"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { AlertTriangle, ChevronLeft, ChevronRight, Search, Users } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { apiFetch, problemDetail } from "@/lib/api";
import { FIELD } from "@/lib/field";
import { cohortLabel, patientHref, recordSummary } from "@/lib/patients";
import type { PatientPage } from "@/types";

const PAGE_SIZE = 25;

// Same debounce the runs index and the audit log use on their search fields:
// the endpoint is on the read rate-limit bucket, and a half-typed name is not a
// query anyone wants answered.
const SEARCH_DEBOUNCE_MS = 300;

/**
 * The synthetic cohort (#96): every patient the Matcher scores, as a collection
 * in its own right rather than as a column of whichever run happened to screen
 * them.
 *
 * Paging and search are server-side, matching the runs index — not because a
 * hundred records need it, but because the client would then hold a second,
 * subtly different idea of what "the cohort" is, and the endpoint is the one
 * that reads the file.
 */
export function PatientsIndex() {
  const [search, setSearch] = useState("");
  const [debounced, setDebounced] = useState("");
  const [offset, setOffset] = useState(0);
  const [page, setPage] = useState<PatientPage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [settledQuery, setSettledQuery] = useState<string | null>(null);

  useEffect(() => {
    const timer = setTimeout(() => setDebounced(search), SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [search]);

  const query = useMemo(() => {
    const params = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(offset) });
    if (debounced) params.set("q", debounced);
    return params.toString();
  }, [offset, debounced]);

  // The offset of the page on screen, as a ref as well as derived below: the
  // failure path has to read it without making the effect depend on the loaded
  // page. Same arrangement as the runs index and the audit log.
  const shownOffsetRef = useRef(0);

  useEffect(() => {
    let active = true;

    const failed = (detail: string) => {
      // Leave the previous page under the error rather than blanking the table —
      // a transient 429 must not read as "there are no patients" — and put the
      // offset back to the page still displayed, so a failed "Next" does not
      // leave the next click skipping the page that failed.
      setError(detail);
      setOffset(shownOffsetRef.current);
      setSettledQuery(query);
    };

    apiFetch(`/api/patients?${query}`)
      .then(async (response) => {
        if (!active) return;
        if (!response.ok) {
          const detail = await problemDetail(response, "Could not load the cohort");
          if (!active) return;
          failed(detail);
          return;
        }
        const body = (await response.json()) as PatientPage;
        if (!active) return;
        setError(null);
        setPage(body);
        shownOffsetRef.current = body.offset;
        setSettledQuery(query);
      })
      .catch(() => {
        if (active) failed("Could not reach the server.");
      });
    return () => {
      active = false;
    };
  }, [query]);

  const loading = settledQuery !== query;
  const rows = page?.items ?? [];
  const total = page?.total ?? 0;
  const shownOffset = page?.offset ?? 0;
  const showingFrom = total === 0 ? 0 : shownOffset + 1;
  const showingTo = shownOffset + rows.length;
  const hasPrev = shownOffset > 0;
  const hasNext = showingTo < total;

  return (
    <div className="space-y-4" data-region="patients-index">
      <div className="relative">
        <Search
          className="text-muted-foreground pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2"
          aria-hidden="true"
        />
        <input
          type="search"
          className={`${FIELD} w-full pl-9`}
          placeholder="Filter by patient id or name"
          aria-label="Filter by patient id or name"
          value={search}
          onChange={(e) => {
            // Paging resets in the handler rather than in an effect watching the
            // filter: staying on offset 50 of a narrower result set shows an
            // empty page that reads as "no patients match".
            setSearch(e.target.value);
            setOffset(0);
            shownOffsetRef.current = 0;
          }}
        />
      </div>

      {error && (
        <Card className="border-destructive/40 bg-destructive/10" role="alert">
          <CardContent className="flex items-start gap-2.5 text-sm">
            <AlertTriangle className="text-destructive mt-0.5 size-4 shrink-0" aria-hidden="true" />
            <span>{error}</span>
          </CardContent>
        </Card>
      )}

      <Card>
        <CardContent>
          {loading && page === null && !error ? (
            <div className="space-y-2" aria-hidden="true">
              <Skeleton className="h-8 w-full" />
              <Skeleton className="h-8 w-full" />
              <Skeleton className="h-8 w-5/6" />
            </div>
          ) : rows.length === 0 && !error ? (
            <div
              className="text-muted-foreground flex flex-col items-center gap-2 py-8 text-center text-sm"
              data-region="patients-empty"
            >
              <Users className="size-5" aria-hidden="true" />
              <span>
                {debounced
                  ? "No patient matches that id or name."
                  : "No patient records are loaded. Generate the synthetic EHR to populate the cohort."}
              </span>
            </div>
          ) : (
            // Scrolls inside its own container so a narrow viewport never scrolls
            // the whole page sideways.
            <div className="overflow-x-auto">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Patient</TableHead>
                    <TableHead>Cohort</TableHead>
                    <TableHead>Age</TableHead>
                    <TableHead>Sex</TableHead>
                    <TableHead>Record</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {rows.map((patient) => (
                    <TableRow key={patient.id} data-cohort={patient.cohort}>
                      <TableCell>
                        <Link
                          href={patientHref(patient.id)}
                          className="hover:text-primary font-medium underline-offset-4 hover:underline"
                        >
                          {patient.name || patient.id}
                        </Link>
                        <span className="text-muted-foreground block font-mono text-xs">
                          {patient.id}
                        </span>
                      </TableCell>
                      <TableCell>
                        <Badge variant="secondary">{cohortLabel(patient.cohort)}</Badge>
                      </TableCell>
                      {/* An em dash, not "0": a record with no age on file is a
                          gap the Matcher reads as "could not be checked", and a
                          zero there would read as a measurement. */}
                      <TableCell>{patient.age ?? "—"}</TableCell>
                      <TableCell>{patient.sex || "—"}</TableCell>
                      <TableCell className="text-muted-foreground text-sm">
                        {recordSummary(patient)}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
        </CardContent>
      </Card>

      {(hasPrev || hasNext) && (
        <div className="flex items-center justify-between gap-3" data-region="patients-pagination">
          <p className="text-muted-foreground text-sm" aria-live="polite">
            Showing {showingFrom}–{showingTo} of {total}
          </p>
          <div className="flex gap-2">
            {/* Both step from the page on screen rather than from the requested
                offset, so they always move exactly one page from what is visible. */}
            <Button
              variant="outline"
              onClick={() => setOffset(Math.max(0, shownOffset - PAGE_SIZE))}
              disabled={!hasPrev || loading}
            >
              <ChevronLeft aria-hidden="true" />
              Previous
            </Button>
            <Button
              variant="outline"
              onClick={() => setOffset(shownOffset + PAGE_SIZE)}
              disabled={!hasNext || loading}
            >
              Next
              <ChevronRight aria-hidden="true" />
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
