"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { AlertTriangle, FlaskConical } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ViewModeToggle } from "@/components/view-mode-toggle";
import { PatientDetailSkeleton } from "@/components/skeletons";
import { useViewMode } from "@/hooks/useViewMode";
import { apiFetch, problemDetail } from "@/lib/api";
import { cohortVariant } from "@/lib/cohort";
import { cohortLabel, deciding, labLabel, sourceNote } from "@/lib/patients";
import { formatTimestamp, runHref } from "@/lib/runs";
import { cn } from "@/lib/utils";
import type { CohortBucketName, PatientRecord, ReverseMatch, TrialMatch } from "@/types";

/** How each bucket reads as a heading — the cohort table's vocabulary, transposed
 *  from patients-per-trial to trials-per-patient. */
const BUCKET_HEADINGS: Record<CohortBucketName, string> = {
  eligible: "Eligible",
  review: "Needs review",
  ineligible: "Ineligible",
};

/**
 * One patient, and every trial they have been put to (#96).
 *
 * The transpose of the run detail page: that one is a protocol with a cohort
 * under it, this is a patient with protocols under them. Both read the same
 * verdicts — for a run that scored this patient, the rows here *are* that run's
 * cohort table rows, replayed rather than recomputed.
 *
 * One request fills the whole page. `GET /api/patients/{id}/trials` returns the
 * record alongside the verdicts, so the record and the matches cannot be a
 * frame apart, and there is no second spinner halfway down.
 */
/**
 * What has been loaded, and *which patient it is about*.
 *
 * The id travels with the result rather than living in a separate state slot:
 * the page is reachable by in-app navigation from one patient to another, and a
 * result stored on its own would render under the new id for as long as the new
 * request is in flight — a previous patient's record, or worse a stale "not
 * found" banner, shown as though it were this one's. Pairing them lets the stale
 * case be *derived* during render instead of cleared by an effect that has
 * already let one frame through.
 */
type Loaded = { id: string; match?: ReverseMatch; error?: string };

export function PatientDetail() {
  const params = useSearchParams();
  const patientId = params.get("id") ?? "";
  const [loaded, setLoaded] = useState<Loaded | null>(null);

  useEffect(() => {
    if (!patientId) return;
    let active = true;
    apiFetch(`/api/patients/${encodeURIComponent(patientId)}/trials`)
      .then(async (response) => {
        if (!active) return;
        if (!response.ok) {
          const detail = await problemDetail(response, "Could not load this patient");
          if (active) setLoaded({ id: patientId, error: detail });
          return;
        }
        const body = (await response.json()) as ReverseMatch;
        if (active) setLoaded({ id: patientId, match: body });
      })
      .catch(() => {
        if (active) setLoaded({ id: patientId, error: "Could not reach the server." });
      });
    return () => {
      active = false;
    };
  }, [patientId]);

  // Anything loaded for a different patient is not this page's answer yet, so it
  // reads as "still loading" rather than as this patient's record.
  const current = loaded?.id === patientId ? loaded : null;

  // A link with no id is a fact about the URL, derived during render rather than
  // pushed into state from the effect: there is nothing to fetch and nothing to
  // wait for, and setting it in an effect would paint the skeleton for a frame
  // first.
  const problem = patientId
    ? current?.error
    : "No patient id in the link. Open a patient from the cohort.";

  if (problem) {
    return (
      <Card className="border-destructive/40 bg-destructive/10" role="alert">
        <CardContent className="flex items-start gap-2.5 text-sm">
          <AlertTriangle className="text-destructive mt-0.5 size-4 shrink-0" aria-hidden="true" />
          <span>{problem}</span>
        </CardContent>
      </Card>
    );
  }

  if (!current?.match) return <PatientDetailSkeleton />;

  return (
    <div className="space-y-4" data-region="patient-detail">
      <PatientCard patient={current.match.patient} />
      <TrialMatches match={current.match} />
    </div>
  );
}

/**
 * The record itself — what the Matcher reads when it scores this patient.
 *
 * Labs, diagnoses, medications and history are shown in full rather than
 * summarized: this is the page a coordinator opens to check *why* a verdict came
 * out the way it did, and the explanations beside it quote these values back.
 */
function PatientCard({ patient }: { patient: PatientRecord }) {
  const labs = Object.entries(patient.labs).filter(([, value]) => value !== undefined);
  return (
    <Card data-region="patient-record">
      <CardHeader>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle className="text-base">{patient.name || patient.id}</CardTitle>
          <div className="flex flex-wrap gap-1.5">
            <Badge variant="secondary">{cohortLabel(patient.cohort)}</Badge>
            {patient.sex && <Badge variant="outline">{patient.sex}</Badge>}
          </div>
        </div>
        <p className="text-muted-foreground font-mono text-xs">{patient.id}</p>
      </CardHeader>
      <CardContent className="space-y-4">
        {labs.length > 0 && (
          <section>
            <h3 className="mb-2 text-sm font-medium">Labs</h3>
            {/* A grid rather than a table: these are name/value pairs with no
                relationship down a column, and a two-column table of eleven rows
                is a lot of chrome for a fact sheet. */}
            <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-sm sm:grid-cols-3 lg:grid-cols-4">
              {labs.map(([attribute, value]) => (
                <div key={attribute}>
                  <dt className="text-muted-foreground text-xs">{labLabel(attribute)}</dt>
                  <dd className="font-medium tabular-nums">{value}</dd>
                </div>
              ))}
            </dl>
          </section>
        )}
        <TermList label="Diagnoses" terms={patient.diagnoses} />
        <TermList label="Medications" terms={patient.medications} />
        <TermList label="History" terms={patient.history} />
      </CardContent>
    </Card>
  );
}

/**
 * One section of the record's free-text terms.
 *
 * An empty section renders as "None on file" rather than disappearing: whether a
 * patient has *no* prior treatments or whether the section is simply missing is
 * exactly the distinction an exclusion criterion turns on, and a section that
 * vanished would leave the reader unable to tell.
 */
function TermList({ label, terms }: { label: string; terms: string[] }) {
  return (
    <section>
      <h3 className="mb-2 text-sm font-medium">{label}</h3>
      {terms.length === 0 ? (
        <p className="text-muted-foreground text-sm">None on file</p>
      ) : (
        <div className="flex flex-wrap gap-1.5">
          {terms.map((term, i) => (
            <Badge key={i} variant="outline" className="max-w-full">
              <span className="truncate">{term}</span>
            </Badge>
          ))}
        </div>
      )}
    </section>
  );
}

/** Every trial this patient was put to, grouped by verdict. */
function TrialMatches({ match }: { match: ReverseMatch }) {
  const { technical } = useViewMode();
  const buckets: CohortBucketName[] = ["eligible", "review", "ineligible"];

  return (
    <Card data-region="patient-trials">
      <CardHeader>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle className="text-base">Trials</CardTitle>
          <ViewModeToggle />
        </div>
        {/* Counts up front, in the cohort table's own three chips — this is that
            table's reduction with the axes swapped, and it should read as one. */}
        <div className="flex flex-wrap gap-1.5">
          <Badge variant="pass">{match.counts.eligible} eligible</Badge>
          <Badge variant="warn">{match.counts.review} need review</Badge>
          <Badge variant="fail">{match.counts.ineligible} ineligible</Badge>
        </div>
        {/* The window the answer was reached in, stated rather than implied: a
            trial outside it is a missed match, and a reader has no other way to
            know the walk was bounded. Only said when it actually bounded
            something — "scanned 3 of 3" is noise. */}
        {match.scanned < match.total && (
          <p className="text-muted-foreground text-sm">
            Checked the {match.scanned} most recent runs of {match.total}.
          </p>
        )}
      </CardHeader>
      <CardContent className="space-y-6">
        {match.trials.length === 0 ? (
          <div
            className="text-muted-foreground flex flex-col items-center gap-2 py-8 text-center text-sm"
            data-region="patient-trials-empty"
          >
            <FlaskConical className="size-5" aria-hidden="true" />
            <span>
              This patient has not been put to any trial yet. A protocol appears here once a
              reviewer approves its criteria at the gate.
            </span>
          </div>
        ) : (
          buckets.map((bucket) => {
            const trials = match.trials.filter((trial) => trial.bucket === bucket);
            if (!trials.length) return null;
            return (
              <section key={bucket} data-bucket={bucket} className="space-y-2">
                <h3 className="flex items-center gap-2 text-sm font-medium">
                  <Badge variant={cohortVariant(bucket)}>{BUCKET_HEADINGS[bucket]}</Badge>
                  <span className="text-muted-foreground">{trials.length}</span>
                </h3>
                {trials.map((trial) => (
                  <TrialRow key={trial.thread_id} trial={trial} technical={technical} />
                ))}
              </section>
            );
          })
        )}
      </CardContent>
    </Card>
  );
}

/**
 * One trial, and the criteria that decided it.
 *
 * The deciding criteria are the ones that did not pass — the same rule the
 * cohort table uses, and for the same reason: a passing criterion is not why the
 * patient landed where they did. An eligible patient shows none, which is the
 * right answer rather than an empty state.
 */
function TrialRow({ trial, technical }: { trial: TrialMatch; technical: boolean }) {
  const reasons = deciding(trial);
  // A run screened before #52 has no plain-language layer, so plain mode falls
  // back to the technical chips rather than rendering a blank — the same
  // fallback `PatientMatchTable` makes.
  const plain = !technical && Boolean(trial.summary);

  return (
    <div className="rounded-md border p-3" data-source={trial.source}>
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <Link
          href={runHref(trial.thread_id)}
          className="hover:text-primary font-medium underline-offset-4 hover:underline"
        >
          {trial.trial_title}
        </Link>
        <span className="text-muted-foreground text-xs">{formatTimestamp(trial.created_at)}</span>
      </div>
      <p className="text-muted-foreground text-xs">{trial.source_filename}</p>

      {plain && <p className="mt-2 text-sm">{trial.summary}</p>}

      {reasons.length > 0 && (
        <div className={cn("mt-2", plain ? "space-y-1" : "flex flex-wrap gap-1")}>
          {plain
            ? reasons
                .filter((result) => result.explanation)
                .map((result, i) => (
                  <p key={i} className="text-muted-foreground text-xs">
                    {result.explanation}
                  </p>
                ))
            : reasons.map((result, i) => (
                <Badge
                  key={i}
                  variant={result.status === "fail" ? "fail" : "warn"}
                  className="max-w-full"
                >
                  <span className="truncate">
                    {result.criterion.source_text} ({result.status})
                  </span>
                </Badge>
              ))}
        </div>
      )}

      {/* Where this verdict came from. Shown only for a rematch: "the run scored
          this patient" is the default a reader is entitled to assume, and
          annotating every row with it would bury the one row where it is not
          true. */}
      {trial.source === "rematched" && (
        <p className="text-muted-foreground mt-2 border-t pt-2 text-xs">{sourceNote(trial)}</p>
      )}
    </div>
  );
}
