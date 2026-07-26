import js from "@eslint/js";
import tseslint from "typescript-eslint";
// Next's own rules (App Router conventions, hooks, jsx-a11y, Core Web Vitals).
// `next lint` was removed in Next 16, so ESLint runs directly against this file.
import next from "eslint-config-next/core-web-vitals";

export default tseslint.config(
  // Build output and the Next-generated ambient types are not ours to lint.
  { ignores: [".next", "out", "node_modules", "next-env.d.ts"] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  ...next,
  {
    files: ["**/*.{ts,tsx}"],
    rules: {
      // Acceptance criterion for #47: the shared backend contract in types.ts is
      // only worth anything if nothing can opt out of it.
      "@typescript-eslint/no-explicit-any": "error",
      "@typescript-eslint/no-unused-vars": ["error", { argsIgnorePattern: "^_" }],
    },
  }
);
