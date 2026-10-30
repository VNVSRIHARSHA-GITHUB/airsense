#!/usr/bin/env bash
# Start Airflow for AirSense (Stage 9): scheduler, DAG processor, triggerer and
# web UI in one process (`airflow standalone`), on http://localhost:8081.
#
# Airflow lives in its own virtual environment (default ~/airflow-venv), not in the
# project's .venv: its pinned versions of FastAPI, pydantic, alembic, grpcio, ...
# differ from the ones the pipeline and the serving image were tested with.
# Its tasks run the pipeline with the project's own environment (.venv/bin/dvc).
#
# Run from anywhere:  bash airflow/start.sh      (stop with Ctrl+C)
set -euo pipefail
PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
AIRFLOW_VENV="${AIRFLOW_VENV:-$HOME/airflow-venv}"

# Airflow's own files (SQLite database, logs, config, admin password): outside the repo.
export AIRFLOW_HOME="${AIRFLOW_HOME:-$HOME/airflow-airsense}"
export AIRFLOW__CORE__DAGS_FOLDER="$PROJECT/airflow/dags"     # the DAGs are part of the project
export AIRFLOW__CORE__LOAD_EXAMPLES=False                      # no example DAGs in the UI
export AIRFLOW__API__PORT=8081                                 # 8080 is the AirSense web page (nginx)
export AIRFLOW__API__BASE_URL=http://localhost:8081
export AIRFLOW__CORE__EXECUTION_API_SERVER_URL=http://localhost:8081/execution/
export AIRFLOW__CORE__DEFAULT_TIMEZONE=Asia/Kolkata            # schedules and UI in IST
export AIRSENSE_PROJECT="$PROJECT"                             # read by the DAGs

# `airflow standalone` starts each component by running `airflow` again, found
# through PATH, so Airflow's environment must come first in PATH.
export PATH="$AIRFLOW_VENV/bin:$PATH"

mkdir -p "$AIRFLOW_HOME"
exec airflow standalone
