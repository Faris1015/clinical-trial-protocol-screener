/**
 * Shared vocabulary for the patient views (#96) — the cohort index, the patient
 * detail page, and the cohort table that links into them.
 *
 * Same arrangement as `lib/runs.ts` and for the same reason: three surfaces need
 * to build the same URL and name the same things, and three copies of that would
 * drift.
 */

import type { PatientSummary, TrialMatch } from "@/types";

/**
 * Deep link to one patient's view.
 *
 * A query parameter rather than a `/patients/<id>` path segment, for the reason
 * `runHref` documents at length: the frontend is a static export, so a dynamic
 * segment would need every id enumerated at build time. The trailing slash is
 * load-bearing the same way — `trailingSlash: true` exports
 * `patients/view/index.html`, and without the slash the client router asks for
 * `patients/view.txt` and takes a full page reload into a 404.
 */
export function patientHref(patientId: string): string {
  return `/patients/view/?id=${encodeURIComponent(patientId)}`;
}

/**
 * The generator's cohort tag as a clinical label.
 *
 * The EHR draws patients from three populations (see
 * `backend/app/data/generate_ehr.py`), and the raw tag is a generator implementation
 * detail — "general" in particular reads as a shrug rather than as what it is,
 * which is the non-trial background population. An unrecognized tag renders as
 * itself: a cohort added to the generator should show up as a new label, not as
 * a blank cell.
 */
const COHORT_LABELS: Record<string, string> = {
  oncology: "Oncology",
  metabolic: "Metabolic",
  general: "Background",
};

export function cohortLabel(cohort: string): string {
  return COHORT_LABELS[cohort] ?? cohort;
}

/**
 * How a lab attribute is named to a reader.
 *
 * Mirrors `ATTRIBUTE_LABELS` in `backend/app/graph/nodes/matcher.py`, the same
 * arrangement (and the same reason) as `lib/cohort.ts` mirroring
 * `services/cohort.py`: the Matcher renders these words into every explanation it
 * writes, and the patient page prints the raw record *beside* those explanations.
 * Left unmapped, the labs grid reads "egfr 58" directly above "The patient's eGFR
 * is 58" — one page naming the same measurement two ways.
 *
 * The backend's copy is written for mid-sentence use, so it is the one that
 * decides capitalization; this is a lookup of it, not a second opinion. An
 * attribute the generator adds later falls through to its own name with the
 * underscores opened out, which is a readable label rather than a blank.
 */
const LAB_LABELS: Record<string, string> = {
  age: "age",
  egfr: "eGFR",
  creatinine: "creatinine",
  systolic_bp: "systolic blood pressure",
  diastolic_bp: "diastolic blood pressure",
  hba1c: "HbA1c",
  bmi: "BMI",
  anc: "neutrophil count",
  platelets: "platelet count",
  ecog: "ECOG performance status",
  ejection_fraction: "ejection fraction",
};

export function labLabel(attribute: string): string {
  return LAB_LABELS[attribute] ?? attribute.replace(/_/g, " ");
}

/**
 * How a patient's record reads in one line — "3 diagnoses · 2 medications".
 *
 * Empty sections are dropped rather than shown as "0 medications": the index is
 * scanned down a column, and a row of zeroes is noise where an absence is the
 * default. A record with nothing on file at all says so.
 */
export function recordSummary(patient: PatientSummary): string {
  const parts = [
    [patient.diagnoses, "diagnosis", "diagnoses"],
    [patient.medications, "medication", "medications"],
    [patient.history, "history entry", "history entries"],
  ] as const;
  const shown = parts
    .filter(([count]) => count > 0)
    .map(([count, one, many]) => `${count} ${count === 1 ? one : many}`);
  return shown.length ? shown.join(" · ") : "Nothing on file";
}

/**
 * The criteria that decided a verdict — everything that did not pass.
 *
 * The same rule the cohort table applies (`PatientMatchTable.unresolved`): a
 * passing criterion is not why a patient landed where they did, and listing all
 * twenty of them would bury the two that mattered. An eligible patient has none,
 * which is the correct answer to "what decided this".
 */
export function deciding(trial: TrialMatch) {
  return trial.criterion_results.filter((result) => result.status !== "pass");
}

/**
 * Whether this verdict is the run's own answer or one derived here.
 *
 * Phrased as a sentence rather than a chip label because the distinction needs
 * the *why*: "rematched" alone would read as a status, and a reader has no way
 * to guess that it means the run never saw this patient.
 */
export function sourceNote(trial: TrialMatch): string {
  if (trial.source === "recorded") {
    return "This run scored this patient directly — the verdict is the one in its own cohort table.";
  }
  const base =
    "This run never saw this patient, so its approved criteria were applied to them here, " +
    "reusing the term matches the run resolved. No model was called.";
  if (trial.unmapped === 0) return base;
  const criteria = trial.unmapped === 1 ? "criterion" : "criteria";
  return (
    `${base} ${trial.unmapped} ${criteria} could not be settled from what the run recorded — ` +
    "the patient's records mention terms it never had to judge, so those need a human."
  );
}
