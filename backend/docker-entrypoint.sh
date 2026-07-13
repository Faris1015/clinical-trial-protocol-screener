#!/bin/sh
# Generate the synthetic patient EHR on first start if the data volume is empty,
# then hand off to the CMD (uvicorn). Seeded + idempotent: a restart with an
# existing volume skips regeneration and keeps the same demo data.
set -e

PATIENTS_PATH="${PATIENTS_PATH:-/data/patients.json}"

if [ ! -f "$PATIENTS_PATH" ]; then
    echo "No patient data at $PATIENTS_PATH — generating synthetic EHR..."
    python -m app.data.generate_ehr
fi

exec "$@"
