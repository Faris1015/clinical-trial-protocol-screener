# Multi-stage: a builder resolves dependencies into a venv, the runtime copies
# only that venv + app source. Keeps the final image slim and free of build
# tooling. Base is pinned to a digest-stable tag matching CI's Python 3.11.
FROM python:3.11-slim AS builder

# Deps go into a self-contained venv so the whole tree can be copied verbatim
# into the runtime stage without dragging pip's build caches along.
RUN python -m venv /venv
ENV PATH="/venv/bin:$PATH"

# Install from the fully-pinned lock (generated via pip-compile from
# pyproject.toml) so builds are reproducible. Only runtime deps are here —
# dev tooling (pytest/ruff/mypy) is excluded from the image.
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock \
    # pip/setuptools/wheel are build-time only — the runtime never installs
    # packages. Dropping them (plus bundled bytecode) trims ~30MB.
    && pip uninstall -y pip setuptools wheel \
    && find /venv -name '__pycache__' -type d -prune -exec rm -rf {} +


FROM python:3.11-slim AS runtime

# Non-root: the app never needs to write outside /data (a mounted volume).
RUN useradd --create-home --uid 1000 appuser

COPY --from=builder /venv /venv
ENV PATH="/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # Patient data lives on a writable volume, not baked into the image — the
    # entrypoint generates it on first start if the volume is empty.
    PATIENTS_PATH=/data/patients.json

WORKDIR /app
COPY --chown=appuser:appuser app ./app
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh \
    && mkdir -p /data \
    && chown appuser:appuser /data

# Commit SHA of the build, surfaced through config.git_sha so /health and
# /ready report exactly which build is live. Set by CI (--build-arg GIT_SHA=…);
# an unset arg leaves git_sha None and the probes report "unknown". Placed after
# the source COPY so a new SHA only busts this trailing layer, not the venv.
ARG GIT_SHA=""
ENV GIT_SHA=$GIT_SHA

USER appuser
EXPOSE 8000

# Liveness for compose depends_on ordering. Uses stdlib (slim has no curl).
# 127.0.0.1, not localhost — uvicorn binds IPv4 only, so an IPv6 (::1) probe
# would fail.
HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)"

ENTRYPOINT ["docker-entrypoint.sh"]
# Bind $PORT when the platform injects one (Render, Cloud Run, HF Spaces),
# defaulting to 8000 so compose and Fly (which fix the port at 8000) are
# unchanged. Shell form + exec so uvicorn still replaces the shell as PID 1.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
