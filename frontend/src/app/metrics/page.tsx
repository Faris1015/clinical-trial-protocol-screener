import type { Metadata } from "next";
import { PageHeader } from "@/components/shell/page-header";
import { PendingRoute } from "@/components/shell/pending-route";

export const metadata: Metadata = { title: "Metrics · TrialGate" };

export default function MetricsPage() {
  return (
    <>
      <PageHeader
        title="Metrics"
        description="Throughput, parse accuracy and escalation rates across screenings."
      />
      <PendingRoute
        title="The metrics dashboard"
        issue={58}
        summary="An in-app summary of the metrics the backend already exports to Prometheus."
      />
    </>
  );
}
