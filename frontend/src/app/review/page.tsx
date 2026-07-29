import type { Metadata } from "next";
import { PageHeader } from "@/components/shell/page-header";
import { ReviewQueue } from "@/components/review/review-queue";

export const metadata: Metadata = { title: "Review Queue · TrialGate" };

export default function ReviewPage() {
  return (
    <>
      <PageHeader
        title="Review Queue"
        description="Screenings parked at the approval gate or escalated for human review."
      />
      <ReviewQueue />
    </>
  );
}
