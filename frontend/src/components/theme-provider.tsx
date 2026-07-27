"use client";

import { ThemeProvider as NextThemesProvider } from "next-themes";

/**
 * Light/dark theming via next-themes, writing `class="dark"` onto <html> — the
 * signal `@custom-variant dark` in globals.css keys off.
 *
 * Two things this depends on, both already true here:
 * - next-themes injects a small inline script that applies the stored theme
 *   before first paint (no flash). The nginx CSP allows `script-src 'unsafe-inline'`
 *   for the static export's RSC payload, so this script is permitted too; a
 *   nonce-only CSP would block it and the page would always paint light first.
 * - <html> carries suppressHydrationWarning in layout.tsx, because that script
 *   mutates the class before React hydrates.
 *
 * `defaultTheme="dark"` preserves the product's existing look for a first-time
 * visitor; `enableSystem` lets the OS preference win when there's no stored choice.
 */
export function ThemeProvider({ children }: { children: React.ReactNode }) {
  return (
    <NextThemesProvider
      attribute="class"
      defaultTheme="dark"
      enableSystem
      disableTransitionOnChange
    >
      {children}
    </NextThemesProvider>
  );
}
