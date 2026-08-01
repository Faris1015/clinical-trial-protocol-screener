import type { Metadata } from "next";
import "./globals.css";
import { ThemeProvider } from "@/components/theme-provider";
import { TooltipProvider } from "@/components/ui/tooltip";
import { AuthProvider } from "@/components/AuthProvider";
import { MotionProvider } from "@/components/motion";
import { AppShell } from "@/components/shell/app-shell";

export const metadata: Metadata = {
  title: "TrialGate",
  description:
    "Multi-agent clinical trial protocol screening — LangGraph, deterministic validation, human-in-the-loop.",
};

/**
 * The dashboard shell every route renders inside: persistent sidebar, sticky top
 * bar, and the scrolling main column. Still a server component — the interactive
 * pieces (nav highlighting, theme, menus) are client islands beneath it.
 *
 * Typography is a system stack declared in globals.css rather than a webfont.
 * `shadcn init` wires up `next/font/google` here by default, which fetches the
 * font during `next build`; both container images build the frontend, so that
 * would make image builds depend on network egress to Google. The stack also
 * needs no CSP allowance, since there is no external font origin.
 *
 * suppressHydrationWarning is required by next-themes, which sets the theme class
 * on <html> before React hydrates. It applies to this element only, not to the
 * tree below it.
 *
 * The shell itself moved into `AppShell` with auth (#50): which chrome to render
 * depends on whether there's a session — a signed-out visitor gets the bare login
 * screen, not a sidebar of nav they can't use — and that is a client-side
 * decision. `AuthProvider` sits above it so the shell, the nav's role gating, and
 * the account menu all read one session check.
 *
 * `MotionProvider` (#49) is the outermost of the client providers because it is
 * the only one every animated element must sit under: it loads the animation
 * features `m.*` renders with and sets the app-wide reduced-motion policy. See
 * components/motion.tsx for why it is `m` + LazyMotion rather than `motion`.
 */
export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body>
        <ThemeProvider>
          <MotionProvider>
            <TooltipProvider>
              <AuthProvider>
                <AppShell>{children}</AppShell>
              </AuthProvider>
            </TooltipProvider>
          </MotionProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
