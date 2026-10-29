#!/usr/bin/env bash
# MLflow model server for the champion model (Stage 6; container-ready in Stage 8).
# Serves models:/airsense-pm25@champion over REST: POST /invocations, GET /ping.
# Laptop: run from the project root with the venv active:  bash model_server/serve.sh
# Docker: started by docker-compose.yml (service model-server).
# A new champion (Stage 5 registry) is picked up by restarting this server.
#
# The champion is first copied to a temporary folder and served from there:
# MLflow writes a small file (registered_model_meta) into the folder it serves,
# and in the container the model store (mlruns/) is mounted read-only.
set -euo pipefail
cd "$(dirname "$0")/.."
export MLFLOW_TRACKING_URI="${MLFLOW_TRACKING_URI:-sqlite:///$(pwd)/mlflow.db}"
export MLFLOW_DISABLE_AGENT_HINT=1
MODEL_URI="models:/airsense-pm25@champion"

MODEL_DIR="$(mktemp -d)"
mlflow artifacts download --artifact-uri "$MODEL_URI" --dst-path "$MODEL_DIR" >/dev/null
echo "Serving $MODEL_URI from a copy in $MODEL_DIR"

exec mlflow models serve \
    -m "$MODEL_DIR" \
    --host "${MODEL_SERVER_HOST:-127.0.0.1}" \
    --port "${MODEL_SERVER_PORT:-5002}" \
    --env-manager local   # use the installed packages; do not build a new environment
