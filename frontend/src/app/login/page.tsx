"use client";

import { useState } from "react";
import { AlertTriangle, ShieldCheck } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { useAuth } from "@/components/AuthProvider";
import { FIELD } from "@/lib/field";

const INPUT = `${FIELD} w-full`;

/**
 * The login page — the only route reachable without a session (#50).
 *
 * Renders outside the dashboard shell (see AppShell): a signed-out visitor gets
 * this card centred on the page, with no sidebar or account menu around it.
 *
 * On success the auth context flips to "authenticated" and AppShell redirects to
 * the dashboard, so there is no navigation to do here.
 */
export default function LoginPage() {
  const { signIn } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await signIn(email, password);
    } catch (problem) {
      // The API deliberately returns one message for a wrong email and a wrong
      // password, so whatever it says is what the user should see.
      setError(problem instanceof Error ? problem.message : "Sign-in failed");
      setSubmitting(false);
    }
    // On success this component unmounts with the redirect, so `submitting` stays
    // true and the button can't be fired a second time in between.
  }

  return (
    <Card className="w-full max-w-sm" data-region="login">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <ShieldCheck className="text-primary size-4" aria-hidden="true" />
          Sign in to TrialGate
        </CardTitle>
        <CardDescription>
          Patient matching is gated to authorized reviewers. Sign in to review protocol criteria and
          clear the approval gate.
        </CardDescription>
      </CardHeader>

      <CardContent>
        <form onSubmit={submit} className="flex flex-col gap-4">
          <div className="flex flex-col gap-1.5">
            <label htmlFor="email" className="text-sm font-medium">
              Email
            </label>
            <input
              id="email"
              type="email"
              className={INPUT}
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              autoComplete="username"
              placeholder="reviewer@example.com"
              required
              autoFocus
            />
          </div>

          <div className="flex flex-col gap-1.5">
            <label htmlFor="password" className="text-sm font-medium">
              Password
            </label>
            <input
              id="password"
              type="password"
              className={INPUT}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="current-password"
              required
            />
          </div>

          {error && (
            <div
              role="alert"
              data-region="login-error"
              className="border-destructive/40 bg-destructive/10 flex items-start gap-2.5 rounded-lg border p-3 text-sm"
            >
              <AlertTriangle
                className="text-destructive mt-0.5 size-4 shrink-0"
                aria-hidden="true"
              />
              <span>{error}</span>
            </div>
          )}

          <Button type="submit" size="lg" className="w-full" disabled={submitting}>
            {submitting ? "Signing in…" : "Sign in"}
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}
