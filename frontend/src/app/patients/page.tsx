import type { Metadata } from "next";
import { PageHeader } from "@/components/shell/page-header";
import { PatientsIndex } from "@/components/patients/patients-index";

export const metadata: Metadata = { title: "Cohort · TrialGate" };

/**
 * The synthetic cohort (#96).
 *
 * Server component only so the route can export `metadata`; every row comes from
 * a client-side fetch against the session cookie. No Suspense boundary — nothing
 * here reads `useSearchParams`, so the component owns its own loading state and
 * the static export has nothing to complain about.
 */
export default function PatientsPage() {
  return (
    <>
      <PageHeader
        title="Cohort"
        description="Every patient in the synthetic EHR. Open one to see which trials they qualify for."
      />
      <PatientsIndex />
    </>
  );
}
