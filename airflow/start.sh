#!/usr/bin/env bash
# Start Airflow for AirSense (Stage 9): scheduler, DAG processor, triggerer and
# web UI in one process (`airflow standalone`), on http://localhost:8081.
# Run from anywhere:  bash airflow/start.sh      (stop with Ctrl+C)
set -euo pipefail
source "$(dirname "$0")/env.sh"

airflow db migrate >/dev/null                                   # create/upgrade the database
# DVC locks the repository, so all DVC tasks share a pool with one slot.
airflow pools set dvc 1 "one DVC command at a time (DVC locks the repository)" >/dev/null
exec airflow standalone
