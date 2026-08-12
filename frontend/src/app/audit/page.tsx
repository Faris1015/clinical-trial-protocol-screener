import type { Metadata } from "next";
import { PageHeader } from "@/components/shell/page-header";
import { AuditIndex } from "@/components/audit/audit-index";

export const metadata: Metadata = { title: "Audit Log · TrialGate" };

/**
 * The org-wide audit log (#98).
 *
 * Server component only so the route can export `metadata`; every row comes from
 * a client-side fetch against the session cookie, and the scope that fetch is
 * answered with depends on the caller's role. No Suspense boundary — nothing here
 * reads `useSearchParams`, so the component owns its own loading state and the
 * static export has nothing to complain about.
 */
export default function AuditPage() {
  return (
    <>
      {/* The description names the *kinds* of decision rather than their reach:
          how far the log reaches depends on the reader's role, and only the
          response knows that — so `AuditIndex` says it, from the scope the server
          applied. A header promising "every run" above a reviewer's own three
          rows would be the page contradicting itself. */}
      <PageHeader
        title="Audit Log"
        description="Approvals, rejections, criteria revisions and escalations — who decided what, and when."
      />
      <AuditIndex />
    </>
  );
}
