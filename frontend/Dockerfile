# Multi-stage: Node builds the static bundle, nginx serves it and reverse-
# proxies /api to the backend so the browser only ever talks to one origin
# (no CORS in the container stack — the proxy makes /api same-origin).
FROM node:22-alpine AS build
WORKDIR /app

# Install against the committed lockfile for reproducible builds.
COPY package.json package-lock.json ./
RUN npm ci

COPY . .
RUN npm run build


FROM nginx:1.27-alpine AS runtime

# Static site config + SPA fallback + /api reverse proxy (incl. SSE tuning).
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80
# Use 127.0.0.1, not localhost: busybox wget tries only the first resolved
# address, and localhost resolves to ::1 first — but nginx `listen 80` binds
# IPv4 only, so an IPv6 probe would fail and mark the container unhealthy.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1:80/ || exit 1

# nginx:alpine's default CMD already runs nginx in the foreground.
