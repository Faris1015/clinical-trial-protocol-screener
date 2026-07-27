import type { Metadata } from "next";
import { PageHeader } from "@/components/shell/page-header";
import { PendingRoute } from "@/components/shell/pending-route";

export const metadata: Metadata = { title: "Rules · TrialGate" };

export default function RulesPage() {
  return (
    <>
      <PageHeader
        title="Rules"
        description="The compliance rules the regulatory critic checks each protocol against."
      />
      <PendingRoute
        title="The rules viewer"
        issue={57}
        summary="A browsable view of the compliance rules database backing the critic's findings."
      />
    </>
  );
}
