"""airsense_retrain: retrain, check and promote the model (Stage 9).

champion_before -> build_training -> build_training_live -> train -> gate -> register
    -> champion_changed? -> restart_serving (model-server, backend) -> wait healthy

Weekly (Sunday 13:00 IST), on demand (Trigger button) and, since Stage 10, when
airsense_monitor finds a high error or drift (at most every 3 days).
build_training_live turns the live data collected so far into training rows, so
every retrain learns from the newest weeks. DVC skips steps whose
inputs did not change, so a run on unchanged data takes seconds and changes
nothing. A model that fails the gate stops the run (red): nothing is
registered and the current champion keeps serving. The containers are restarted
only if the champion version actually changed.
"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

from airflow.providers.standard.operators.bash import BashOperator
from airflow.providers.standard.operators.python import (
    PythonOperator,
    ShortCircuitOperator,
)
from airflow.sdk import DAG

sys.path.insert(0, str(Path(__file__).parent))  # the shared helper next to this file
from airsense_common import PROJECT, dvc_stage, in_project, record_run

REGISTRY_RECORD = Path(PROJECT) / "data" / "interim" / "registry.json"


def champion_version() -> str | None:
    """Champion version from the last decision `register` recorded."""
    try:
        return str(json.loads(REGISTRY_RECORD.read_text())["champion_version"])
    except (OSError, ValueError, KeyError):
        return None


def champion_changed(ti) -> bool:
    """True (continue to the restart) only if `register` changed the champion."""
    before = ti.xcom_pull(task_ids="champion_before")
    after = champion_version()
    print(f"champion before: v{before}, after: v{after}")
    return after is not None and after != before


# Restart model server and backend, then wait until /health reports the new
# champion loaded and every dependency up (at most ~3 min).
RESTART = in_project(
    "docker compose restart model-server backend; "
    "for i in $(seq 36); do "
    "  h=$(curl -s --max-time 4 http://127.0.0.1:8000/health || true); "
    '  echo "$h" | grep -q \'"status":"ok"\' && '
    '  echo "$h" | grep -q \'"champion_up_to_date":true\' && { echo "healthy: $h"; exit 0; }; '
    "  sleep 5; "
    'done; echo "not healthy after restart: $h"; exit 1'
)

with DAG(
    dag_id="airsense_retrain",
    description="Retrain, gate, register; serve the new champion if it changed",
    schedule="0 13 * * 0",  # Sundays 13:00 IST (laptop usually on); plus Trigger
    start_date=datetime(2026, 10, 30),
    catchup=False,
    max_active_runs=1,
    tags=["airsense", "model"],
    doc_md=__doc__,
    on_success_callback=record_run,  # last run for the Pipeline console
    on_failure_callback=record_run,
) as dag:
    before = PythonOperator(task_id="champion_before", python_callable=champion_version)
    changed = ShortCircuitOperator(
        task_id="champion_changed", python_callable=champion_changed
    )
    restart = BashOperator(
        task_id="restart_serving",
        bash_command=RESTART,
        retries=1,
        retry_delay=timedelta(minutes=1),
        execution_timeout=timedelta(minutes=6),
    )
    (
        before
        >> dvc_stage("build_training", timeout_min=60, retries=0)
        >> dvc_stage("build_training_live", timeout_min=20, retries=0)
        >> dvc_stage("train", timeout_min=120, retries=0)  # memory-heavy: no retry
        >> dvc_stage("gate", timeout_min=10, retries=0)
        >> dvc_stage("register", timeout_min=30, retries=0)
        >> changed
        >> restart
    )
