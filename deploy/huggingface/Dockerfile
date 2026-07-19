# Hugging Face Space Dockerfile — single-service demo (React SPA + FastAPI,
# stub LLM) in ONE free container. A Space is its own git repo, so this file is
# self-contained: it clones the project at build time rather than COPYing from a
# build context. Mirrors deploy/demo/Dockerfile in the main repo.
#
# To point the Space at an un-merged branch (e.g. before this lands on main),
# set the REPO_REF build arg in the Space's Variables/Secrets settings.
# Full runbook: docs/free-demo-deploy.md in the main repo.

FROM alpine/git AS source
ARG REPO_URL=https://github.com/Faris1015/clinical-trial-protocol-screener
ARG REPO_REF=main
RUN git clone --depth 1 --branch "${REPO_REF}" "${REPO_URL}" /src

# --- Build the React bundle --------------------------------------------------
FROM node:22-alpine AS web
WORKDIR /web
COPY --from=source /src/frontend/ ./
RUN npm ci && npm run build

# --- Resolve backend deps into a venv ----------------------------------------
FROM python:3.11-slim AS builder
RUN python -m venv /venv
ENV PATH="/venv/bin:$PATH"
COPY --from=source /src/backend/requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock \
    && pip uninstall -y pip setuptools wheel \
    && find /venv -name '__pycache__' -type d -prune -exec rm -rf {} +

# --- Runtime -----------------------------------------------------------------
FROM python:3.11-slim AS runtime
RUN useradd --create-home --uid 1000 appuser

COPY --from=builder /venv /venv
ENV PATH="/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LLM_PROVIDER=stub \
    FRONTEND_DIST=/app/frontend_dist \
    PATIENTS_PATH=/data/patients.json \
    SQLITE_PATH=/data/screenings.sqlite \
    LOG_FORMAT=json

WORKDIR /app
COPY --from=source --chown=appuser:appuser /src/backend/app ./app
COPY --from=web --chown=appuser:appuser /web/dist ./frontend_dist
COPY --from=source /src/backend/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh \
    && mkdir -p /data \
    && chown appuser:appuser /data

USER appuser
# HF routes to the port declared as `app_port` in README.md (8000 here); the
# CMD binds $PORT if the platform injects one, else 8000.
EXPOSE 8000
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]