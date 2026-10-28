#!/usr/bin/env bash
# MLflow model server for the champion model (Stage 6).
# Serves models:/airsense-pm25@champion over REST: POST /invocations, GET /ping.
# Run from the project root with the venv active:  bash model_server/serve.sh
# A new champion (Stage 5 registry) is picked up by restarting this server.
set -euo pipefail
cd "$(dirname "$0")/.."
export MLFLOW_TRACKING_URI="${MLFLOW_TRACKING_URI:-sqlite:///$(pwd)/mlflow.db}"
export MLFLOW_DISABLE_AGENT_HINT=1
exec mlflow models serve \
    -m "models:/airsense-pm25@champion" \
    --host "${MODEL_SERVER_HOST:-127.0.0.1}" \
    --port "${MODEL_SERVER_PORT:-5002}" \
    --env-manager local   # use the project's .venv; do not build a new environment
