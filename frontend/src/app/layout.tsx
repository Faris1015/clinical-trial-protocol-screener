import type { Metadata } from "next";
import "./globals.css";
import { ThemeProvider } from "@/components/theme-provider";
import { TooltipProvider } from "@/components/ui/tooltip";
import { Sidebar } from "@/components/shell/sidebar";
import { TopBar } from "@/components/shell/top-bar";

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
 * `min-w-0` on the main column is load-bearing, not decoration: a flex child
 * defaults to `min-width: auto`, so a wide cohort table would stretch the column
 * past the viewport and scroll the whole body sideways instead of scrolling inside
 * the table's own overflow container.
 *
 * suppressHydrationWarning is required by next-themes, which sets the theme class
 * on <html> before React hydrates. It applies to this element only, not to the
 * tree below it.
 */
export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body>
        <ThemeProvider>
          <TooltipProvider>
            <div className="flex min-h-svh">
              <Sidebar />
              <div className="flex min-w-0 flex-1 flex-col">
                <TopBar />
                <main className="min-w-0 flex-1 p-4 md:p-6">
                  <div className="mx-auto w-full max-w-5xl">{children}</div>
                </main>
              </div>
            </div>
          </TooltipProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
