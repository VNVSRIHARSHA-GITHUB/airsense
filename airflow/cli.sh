#!/usr/bin/env bash
# Run any Airflow command with the AirSense settings, e.g.
#   bash airflow/cli.sh dags list
#   bash airflow/cli.sh dags list-import-errors
#   bash airflow/cli.sh dags trigger airsense_live
set -euo pipefail
source "$(dirname "$0")/env.sh"
exec airflow "$@"
