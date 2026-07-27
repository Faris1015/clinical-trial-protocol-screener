import type { Metadata } from "next";
import { PageHeader } from "@/components/shell/page-header";
import { PendingRoute } from "@/components/shell/pending-route";

export const metadata: Metadata = { title: "Past Runs · TrialGate" };

export default function RunsPage() {
  return (
    <>
      <PageHeader title="Past Runs" description="Every screening this instance has processed." />
      <PendingRoute
        title="Run history"
        issue={51}
        summary="An index of past screenings with deep links into each run's detail view."
      />
    </>
  );
}
