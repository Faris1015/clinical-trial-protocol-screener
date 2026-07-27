import type { Metadata } from "next";
import { PageHeader } from "@/components/shell/page-header";
import { PendingRoute } from "@/components/shell/pending-route";

export const metadata: Metadata = { title: "Review Queue · TrialGate" };

export default function ReviewPage() {
  return (
    <>
      <PageHeader
        title="Review Queue"
        description="Screenings parked at the approval gate or escalated for human review."
      />
      <PendingRoute
        title="The review queue"
        issue={53}
        summary="A reviewer's worklist, with the edit-and-rerun flow for correcting parsed criteria."
      />
    </>
  );
}
