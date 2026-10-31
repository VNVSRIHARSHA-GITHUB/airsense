"""airsense_housekeeping: daily data versioning and clean-up (Stage 9).

version_live_data -> push_data -> commit_checkpoint -> gc_dvc_cache
clean_airflow_logs, prune_old_images (independent)

- The live snapshots (CPCB feed, Open-Meteo forecasts) cannot be downloaded
  again, so once a day they are versioned with `dvc add` and copied to the DVC
  remote with `dvc push`.
- One Git commit per day records the data versions (`*.dvc`, `dvc.lock`) and the
  step reports, instead of 48 changed files every 30 minutes. Only those paths
  are committed (anything else you have staged is left alone); nothing is pushed.
- Then `dvc gc --all-commits` removes cached versions no commit refers to (the
  live outputs are rebuilt every 30 min; only the daily checkpoint is kept).
- Airflow's own task logs older than 14 days and AirSense images not used by a
  running container are removed. The Docker build cache is kept (fast rebuilds).

Daily at 23:15 IST, and on demand (Trigger).
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

from airflow.providers.standard.operators.bash import BashOperator
from airflow.sdk import DAG

sys.path.insert(0, str(Path(__file__).parent))  # the shared helper next to this file
from airsense_common import DVC_POOL, in_project, record_run

LIVE = "data/raw/live/datagov data/raw/live/openmeteo"
# the data versions and the step reports; only paths that exist are passed to Git
CHECKPOINT_PATHS = "dvc.lock data/interim $(ls data/raw/live/*.dvc 2>/dev/null)"

with DAG(
    dag_id="airsense_housekeeping",
    description="Version live data (dvc add + push), daily Git checkpoint, clean-up",
    schedule="15 23 * * *",
    start_date=datetime(2026, 10, 30),
    catchup=False,
    max_active_runs=1,
    tags=["airsense", "housekeeping"],
    doc_md=__doc__,
    on_success_callback=record_run,  # last run for the Pipeline console
    on_failure_callback=record_run,
) as dag:
    version = BashOperator(
        task_id="version_live_data",
        bash_command=in_project(f"for d in {LIVE}; do [ -d $d ] && dvc add $d; done"),
        pool=DVC_POOL,
        execution_timeout=timedelta(minutes=15),
    )
    push = BashOperator(
        task_id="push_data",
        bash_command=in_project("dvc push"),
        pool=DVC_POOL,
        retries=1,
        retry_delay=timedelta(minutes=5),
        execution_timeout=timedelta(minutes=30),
    )
    commit = BashOperator(
        task_id="commit_checkpoint",
        bash_command=in_project(
            f"git add -- {CHECKPOINT_PATHS}; "
            f"if git diff --cached --quiet -- {CHECKPOINT_PATHS}; then "
            "  echo 'nothing new to record'; "
            "else "
            f'  git commit -m "Daily data checkpoint $(date +%F)" -- {CHECKPOINT_PATHS}; '
            "fi"
        ),
        execution_timeout=timedelta(minutes=10),
    )
    # Keeps every version referenced by any Git commit (all checkpoints, models,
    # history) and the current workspace; removes the 30-minute intermediates.
    gc = BashOperator(
        task_id="gc_dvc_cache",
        bash_command=in_project("dvc gc --all-commits --force"),
        pool=DVC_POOL,
        execution_timeout=timedelta(minutes=15),
    )
    clean_logs = BashOperator(
        task_id="clean_airflow_logs",
        bash_command='find "$AIRFLOW_HOME/logs" -type f -mtime +14 -delete; '
        'find "$AIRFLOW_HOME/logs" -type d -empty -delete; echo done',
    )
    # Never removes an image a container uses; does nothing if no AirSense
    # container is running (then "in use" cannot be known).
    prune_images = BashOperator(
        task_id="prune_old_images",
        bash_command=(
            # {% raw %}: Airflow reads {{ }} as its own template syntax; this
            # keeps Docker's --format '{{.Image}}' as it is.
            "{% raw %}"
            "used=$(docker ps --format '{{.Image}}' | grep -E '^airsense-' || true); "
            'if [ -z "$used" ]; then echo "no AirSense containers running: skipped"; exit 0; fi; '
            "docker images --format '{{.Repository}}:{{.Tag}}' "
            "| grep -E '^airsense-(app|frontend):' "
            '| grep -vxF -f <(echo "$used") '
            "| xargs -r docker rmi || true"
            "{% endraw %}"
        ),
    )
    version >> push >> commit >> gc
