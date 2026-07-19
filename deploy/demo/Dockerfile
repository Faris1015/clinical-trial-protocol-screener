# Single-service demo image: one container serving BOTH the React SPA and the
# FastAPI API, in stub-LLM mode — no Ollama, no Anthropic key, no second host,
# no CORS. Built for free/no-credit-card hosts (Hugging Face Spaces, Render).
# Full runbook: docs/free-demo-deploy.md.
#
# BUILD CONTEXT IS THE REPO ROOT (not backend/), so it can pull in both apps:
#   docker build -f deploy/demo/Dockerfile -t screener-demo .
#
# The production topology (backend/Dockerfile + frontend/Dockerfile behind nginx,
# real LLM) is unchanged — this is a separate, demo-only image.

# --- Stage 1: build the React bundle -----------------------------------------
FROM node:22-alpine AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
# The SPA calls the API with relative /api paths, so no API base URL is needed —
# it's same-origin with the FastAPI app that serves this bundle.
RUN npm run build

# --- Stage 2: resolve backend deps into a venv (mirrors backend/Dockerfile) ---
FROM python:3.11-slim AS builder
RUN python -m venv /venv
ENV PATH="/venv/bin:$PATH"
COPY backend/requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock \
    && pip uninstall -y pip setuptools wheel \
    && find /venv -name '__pycache__' -type d -prune -exec rm -rf {} +

# --- Stage 3: runtime --------------------------------------------------------
FROM python:3.11-slim AS runtime
RUN useradd --create-home --uid 1000 appuser

COPY --from=builder /venv /venv
ENV PATH="/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # Demo defaults — zero-inference stub LLM, SPA served from this app, and all
    # writable state on the (ephemeral) /data dir the entrypoint seeds.
    LLM_PROVIDER=stub \
    FRONTEND_DIST=/app/frontend_dist \
    PATIENTS_PATH=/data/patients.json \
    SQLITE_PATH=/data/screenings.sqlite \
    LOG_FORMAT=json

WORKDIR /app
COPY --chown=appuser:appuser backend/app ./app
COPY --from=web --chown=appuser:appuser /web/dist ./frontend_dist
COPY backend/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh \
    && mkdir -p /data \
    && chown appuser:appuser /data

ARG GIT_SHA=""
ENV GIT_SHA=$GIT_SHA

USER appuser
EXPOSE 8000

# Liveness via stdlib (slim has no curl); honors $PORT like the CMD below.
HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
    CMD python -c "import os,urllib.request,sys; p=os.environ.get('PORT','8000'); sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{p}/health').status==200 else 1)"

ENTRYPOINT ["docker-entrypoint.sh"]
# Bind $PORT when the host injects one (Render, Cloud Run); default 8000 (HF
# Spaces app_port, local). Shell form + exec so uvicorn stays PID 1.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]