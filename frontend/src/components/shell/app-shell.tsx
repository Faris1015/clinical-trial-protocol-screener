"use client";

import { useEffect } from "react";
import { usePathname, useRouter } from "next/navigation";
import { Loader2 } from "lucide-react";
import { Sidebar } from "@/components/shell/sidebar";
import { TopBar } from "@/components/shell/top-bar";
import { useAuth } from "@/components/AuthProvider";

/** The one route reachable without a session. */
export const LOGIN_PATH = "/login";

// The same route as a navigation target. The trailing slash is load-bearing, not
// style: `trailingSlash: true` makes the export write `login/index.txt`, so
// navigating to "/login" has the client router fetch `/login.txt`, 404, and fall
// back to a full page load. That hard navigation then hits the static host's
// directory redirect — which under nginx's default `absolute_redirect on` drops
// the published port and lands the browser on a dead origin. Navigating to the
// canonical path keeps it a client-side transition and never asks for either.
const LOGIN_HREF = `${LOGIN_PATH}/`;

// `trailingSlash: true` (next.config.ts) means pathnames arrive as "/login/".
function normalize(pathname: string): string {
  return pathname.length > 1 && pathname.endsWith("/") ? pathname.slice(0, -1) : pathname;
}

/**
 * Chooses the chrome for the current session state, and sends people to the right
 * route (#50).
 *
 * Chrome and access are one decision, not two: a signed-out visitor should see a
 * bare login screen, not a sidebar full of nav they can't use, so whoever picks
 * the layout also has to know whether there's a session. Splitting them left the
 * login page wearing the dashboard's shell.
 *
 * The redirect is client-side by necessity — a static export has no server to run
 * middleware in (see next.config.ts). It is *not* the security boundary and isn't
 * meant to be: the API rejects every unauthenticated request on its own
 * (`Depends(require_reviewer)` in app/main.py), so someone who defeats this guard
 * reaches a shell that cannot load a single byte of screening data. This exists to
 * put people on the right screen, not to keep data safe.
 */
export function AppShell({ children }: { children: React.ReactNode }) {
  const { status } = useAuth();
  const router = useRouter();
  const onLoginPage = normalize(usePathname()) === LOGIN_PATH;

  useEffect(() => {
    if (status === "checking") return;
    if (status === "anonymous" && !onLoginPage) {
      // `replace`, not `push`: the protected URL shouldn't sit in history where
      // Back would bounce the user straight into the redirect again.
      router.replace(LOGIN_HREF);
    }
    if (status === "authenticated" && onLoginPage) {
      router.replace("/");
    }
  }, [status, onLoginPage, router]);

  // The session check is one request, but it gates every route, so it gets a
  // real indicator rather than a blank page.
  if (status === "checking") {
    return (
      <div
        className="flex min-h-svh items-center justify-center"
        role="status"
        data-app-shell="checking"
      >
        <Loader2 className="text-muted-foreground size-5 animate-spin" aria-hidden="true" />
        <span className="sr-only">Checking your session…</span>
      </div>
    );
  }

  // Signed out: only the login screen, and centred with no nav around it. Also
  // covers the frame between "we know you're signed out" and the redirect
  // landing, so a protected page never flashes its chrome.
  if (status === "anonymous") {
    return (
      <div className="flex min-h-svh items-center justify-center p-4" data-app-shell="anonymous">
        {onLoginPage ? children : null}
      </div>
    );
  }

  // Signed in but sitting on /login — render nothing for the frame before the
  // redirect to "/" lands, rather than the login form over a live session.
  if (onLoginPage) return null;

  return (
    // `data-app-shell` marks which branch rendered. The CI smoke test greps the
    // exported HTML for it: since the sidebar is now session-gated, no nav appears
    // in a static file, so this is what proves the auth-aware shell is the boot
    // path rather than the export having silently lost its layout.
    <div className="flex min-h-svh" data-app-shell="authenticated">
      <Sidebar />
      <div className="flex min-w-0 flex-1 flex-col">
        <TopBar />
        {/* `min-w-0`: a flex child defaults to min-width:auto, so a wide cohort
            table would stretch this column past the viewport and scroll the body
            sideways instead of scrolling inside the table's own container. */}
        <main className="min-w-0 flex-1 p-4 md:p-6">
          <div className="mx-auto w-full max-w-5xl">{children}</div>
        </main>
      </div>
    </div>
  );
}
