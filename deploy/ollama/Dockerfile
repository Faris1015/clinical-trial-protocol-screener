# Dedicated Ollama service for the production (Fly) deployment.
#
# The stock ollama/ollama image runs `ollama serve` but never pulls a model —
# on a fresh machine the backend's first screening would 404 on the model. This
# wrapper starts the server, waits for it, pulls ${OLLAMA_MODEL} if absent, then
# hands the server the foreground. The pull is idempotent: the model lives on a
# mounted volume (/root/.ollama), so a restart with a warm volume skips it.
#
# Pinned to the same tag the compose stack uses so local and prod inference match.
FROM ollama/ollama:0.6.2

# Overridable at build/deploy time; keep in sync with the backend's OLLAMA_MODEL.
ENV OLLAMA_MODEL=llama3.1:8b \
    # Listen on all interfaces so the Fly proxy can reach it from the backend
    # app over the private network (the default 127.0.0.1 bind is unreachable).
    OLLAMA_HOST=0.0.0.0:11434

COPY entrypoint.sh /usr/local/bin/ollama-entrypoint.sh
RUN chmod +x /usr/local/bin/ollama-entrypoint.sh

# Base image sets ENTRYPOINT ["/bin/ollama"]; override it with our wrapper.
ENTRYPOINT ["/usr/local/bin/ollama-entrypoint.sh"]