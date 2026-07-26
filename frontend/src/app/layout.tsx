import type { Metadata } from "next";
import "./styles.css";

export const metadata: Metadata = {
  title: "TrialGate",
  description:
    "Multi-agent clinical trial protocol screening — LangGraph, deterministic validation, human-in-the-loop.",
};

/**
 * The application shell every route renders inside: branding, and the centered
 * `.app` column. Deliberately a server component with no interactive state, so
 * the routes added on top of it (`/runs`, `/runs/[id]`, `/review/[id]` — #51,
 * #53) inherit the chrome without re-declaring it. Sidebar navigation lands here
 * in #48.
 */
export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <div className="app">
          <header>
            <h1>TrialGate</h1>
            <p>Multi-agent · LangGraph · deterministic validation · human-in-the-loop</p>
          </header>
          {children}
        </div>
      </body>
    </html>
  );
}
