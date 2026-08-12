import { Suspense } from "react";
import type { Metadata } from "next";
import { PageHeader } from "@/components/shell/page-header";
import { PatientDetailSkeleton } from "@/components/skeletons";
import { PatientDetail } from "@/components/patients/patient-detail";

export const metadata: Metadata = { title: "Patient · TrialGate" };

/**
 * One patient, deep-linked as `/patients/view/?id=<patient_id>` (#96).
 *
 * The Suspense boundary is required, not decorative: `useSearchParams` inside
 * `<PatientDetail>` has no value during prerendering, and `next build` refuses
 * to export a page that reads it without one. Its fallback is the same skeleton
 * the component shows while it fetches, so the exported HTML — which is what a
 * cold deep link paints first — and the hydrated page are the same picture.
 */
export default function PatientDetailPage() {
  return (
    <>
      <PageHeader
        title="Patient"
        description="One patient's record, and every trial their eligibility has been decided against."
      />
      <Suspense fallback={<PatientDetailSkeleton />}>
        <PatientDetail />
      </Suspense>
    </>
  );
}
