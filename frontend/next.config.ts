import type { NextConfig } from "next";

/**
 * The dev-only API upstream. In every deployed topology the browser talks to a
 * single origin that already routes /api to FastAPI (nginx in the compose/GHCR
 * stack, the FastAPI static mount in the single-service demo image), so the
 * rewrite below exists purely to reproduce that shape on `next dev`.
 */
const DEV_API_UPSTREAM = process.env.NEXT_DEV_API_UPSTREAM ?? "http://localhost:8000";

const nextConfig: NextConfig = {
  // Static export: `next build` emits a self-contained `out/` tree instead of a
  // Node server. That is what keeps both container topologies working unchanged
  // — nginx serves `out/` in frontend/Dockerfile, and FastAPI's StaticFiles
  // mount serves it in the single-service demo image (deploy/demo/Dockerfile),
  // so the demo still ships frontend + API from one origin with one process.
  //
  // Trade-off to know before adding routes: an exported app has no request-time
  // server, so dynamic segments need `generateStaticParams` at build time.
  // `/runs` and `/review` (#51, #53) work as-is; `/runs/[id]` for arbitrary
  // thread ids needs either a query param (`/runs/view?id=…`) or dropping this
  // line and running `next start` behind nginx — at which point the demo image
  // needs a second process. The App Router shell here is unchanged either way.
  output: "export",

  // Emit every route as `<route>/index.html` rather than `<route>.html`. Both
  // static hosts resolve a directory to its index.html (nginx via `try_files
  // $uri/`, Starlette's StaticFiles via html=True), so directory-style output is
  // the one layout that works in both without host-specific rewrite rules.
  trailingSlash: true,

  // Paired with trailingSlash above: without this, `next dev` answers
  // `POST /api/screenings` with a 308 to `/api/screenings/`, which FastAPI
  // redirects straight back — an infinite loop on the upload call. The static
  // hosts do their own trailing-slash resolution in production, so nothing here
  // depends on Next issuing that redirect.
  skipTrailingSlashRedirect: true,

  reactStrictMode: true,

  // Spread rather than declared unconditionally: `next build` warns whenever the
  // key is *present* under `output: "export"` (rewrites are a server feature, so
  // the export drops them), regardless of what the function returns. Omitting it
  // outside dev keeps the build output clean.
  ...(process.env.NODE_ENV === "development"
    ? {
        rewrites: async () => [
          { source: "/api/:path*", destination: `${DEV_API_UPSTREAM}/api/:path*` },
        ],
      }
    : {}),
};

export default nextConfig;
