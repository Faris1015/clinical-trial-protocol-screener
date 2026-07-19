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
# Shipped as a template: the nginx image's entrypoint runs envsubst over
# /etc/nginx/templates/*.template at startup and writes the result into
# conf.d/. BACKEND_UPSTREAM defaults to the compose service name; Fly overrides
# it to the backend app's private address. The FILTER limits substitution to
# BACKEND_-prefixed vars so nginx's own $host/$uri runtime variables survive.
ENV BACKEND_UPSTREAM=backend:8000 \
    NGINX_ENVSUBST_FILTER=BACKEND_
COPY default.conf.template /etc/nginx/templates/default.conf.template
# Runs before nginx starts: writes /etc/nginx/resolver.conf so the templated
# config can resolve the backend per-request (see the script + template).
COPY 15-detect-resolver.sh /docker-entrypoint.d/15-detect-resolver.sh
RUN chmod +x /docker-entrypoint.d/15-detect-resolver.sh
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80
# Use 127.0.0.1, not localhost: busybox wget tries only the first resolved
# address, and localhost resolves to ::1 first — but nginx `listen 80` binds
# IPv4 only, so an IPv6 probe would fail and mark the container unhealthy.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1:80/ || exit 1

# nginx:alpine's default CMD already runs nginx in the foreground.
