#!/bin/sh
# Start ollama, ensure the configured model is present, then run the server in
# the foreground as PID 1's child. Idempotent: the model is cached on the
# mounted volume, so only the first boot (cold volume) pays the pull cost.
set -e

MODEL="${OLLAMA_MODEL:-llama3.1:8b}"

# Start the server in the background so we can pull against it.
ollama serve &
server_pid=$!

# Wait for the server to accept connections before pulling (bounded: ~60s).
i=0
until ollama list >/dev/null 2>&1; do
    i=$((i + 1))
    if [ "$i" -ge 30 ]; then
        echo "ollama server did not become ready within 60s" >&2
        exit 1
    fi
    sleep 2
done

# Pull only if the tag isn't already on the volume. `ollama pull` is itself a
# no-op for an up-to-date model, but the guard keeps a warm-start boot quiet.
if ollama list | awk '{print $1}' | grep -qx "$MODEL"; then
    echo "model $MODEL already present — skipping pull"
else
    echo "pulling model $MODEL ..."
    ollama pull "$MODEL"
fi

# Hand the foreground to the server; forward SIGTERM so Fly can stop us cleanly.
trap 'kill -TERM "$server_pid" 2>/dev/null' TERM INT
wait "$server_pid"