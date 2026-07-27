"use client";

import { useSyncExternalStore } from "react";
import { Moon, Sun } from "lucide-react";
import { useTheme } from "next-themes";
import { Button } from "@/components/ui/button";

const noop = () => () => {};

/**
 * True only after hydration. React's documented way to ask "am I on the client
 * yet" — `getServerSnapshot` answers the prerender and the hydration pass, then
 * React re-renders with `getSnapshot`.
 *
 * The `mounted` state + effect this replaces is the more familiar idiom, but it
 * sets state from inside an effect, which react-hooks (shipped by
 * eslint-config-next) rejects as a cascading render.
 */
function useHydrated(): boolean {
  return useSyncExternalStore(
    noop,
    () => true,
    () => false
  );
}

/**
 * Light/dark switch.
 *
 * The gate is not ceremony: this renders during `next build`, when the visitor's
 * stored theme is unknowable, so choosing an icon before hydration renders the
 * wrong one and trips a hydration mismatch. Until then it renders a same-sized
 * placeholder, so resolving the theme doesn't shift the top bar's layout.
 */
export function ThemeToggle() {
  const { resolvedTheme, setTheme } = useTheme();
  const hydrated = useHydrated();

  if (!hydrated) {
    return <div className="size-8" aria-hidden="true" />;
  }

  const isDark = resolvedTheme === "dark";
  return (
    <Button
      variant="ghost"
      size="icon"
      onClick={() => setTheme(isDark ? "light" : "dark")}
      aria-label={`Switch to ${isDark ? "light" : "dark"} theme`}
      title={`Switch to ${isDark ? "light" : "dark"} theme`}
      data-theme-toggle={isDark ? "dark" : "light"}
    >
      {isDark ? (
        <Sun className="size-4" aria-hidden="true" />
      ) : (
        <Moon className="size-4" aria-hidden="true" />
      )}
    </Button>
  );
}
