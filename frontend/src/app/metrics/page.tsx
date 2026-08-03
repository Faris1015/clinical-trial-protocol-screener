import type { Metadata } from "next";
import { PageHeader } from "@/components/shell/page-header";
import { MetricsOverview } from "@/components/metrics/metrics-overview";

export const metadata: Metadata = { title: "Metrics · TrialGate" };

/**
 * The in-app metrics summary (#58).
 *
 * Server component only so the route can export `metadata`; the numbers come from
 * a client-side fetch against the session cookie. No Suspense boundary here,
 * unlike `/rules` — nothing on this page reads `useSearchParams`, so the component
 * owns its own loading state and the static export has nothing to complain about.
 */
export default function MetricsPage() {
  return (
    <>
      <PageHeader
        title="Metrics"
        description="Throughput, parse accuracy and escalation rates across screenings."
      />
      <MetricsOverview />
    </>
  );
}
