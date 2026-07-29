import type { Metadata } from "next";
import { PageHeader } from "@/components/shell/page-header";
import { RunsIndex } from "@/components/runs/runs-index";

export const metadata: Metadata = { title: "Past Runs · TrialGate" };

/**
 * Server component purely so the route can export `metadata` — every byte of
 * the table below comes from a client-side fetch against the session cookie, so
 * the work happens in `<RunsIndex>`.
 */
export default function RunsPage() {
  return (
    <>
      <PageHeader title="Past Runs" description="Every screening this instance has processed." />
      <RunsIndex />
    </>
  );
}
