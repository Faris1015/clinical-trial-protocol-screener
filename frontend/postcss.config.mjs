/**
 * Tailwind v4 runs as a PostCSS plugin — there is no tailwind.config.js. All
 * design tokens live in `@theme` inside src/app/globals.css instead.
 */
const config = {
  plugins: ["@tailwindcss/postcss"],
};

export default config;
