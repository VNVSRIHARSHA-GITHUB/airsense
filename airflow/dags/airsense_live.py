"""airsense_live: fresh data for the web page every 30 minutes (Stage 9).

collect -> process_live -> validate_processed -> build_features_live -> feast_online

Replaces the collector's cron entry and the manual `dvc repro feast_online`.
Runs at minutes 5 and 35 (the feed updates hourly; two tries per hour). Missed
runs (laptop asleep) are not replayed: the feed only has the current hour.
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

from airflow.providers.standard.operators.bash import BashOperator
from airflow.sdk import DAG

sys.path.insert(0, str(Path(__file__).parent))  # the shared helper next to this file
from airsense_common import dvc_stage, in_project, record_run

with DAG(
    dag_id="airsense_live",
    description="Collect live PM2.5, process, validate, build features, refresh Redis",
    schedule="5,35 * * * *",
    start_date=datetime(2026, 10, 30),
    catchup=False,  # do not replay runs missed while the laptop was off
    max_active_runs=1,  # a slow run never overlaps the next one
    dagrun_timeout=timedelta(minutes=29),
    tags=["airsense", "live"],
    doc_md=__doc__,
    on_success_callback=record_run,  # last run for the Pipeline console
    on_failure_callback=record_run,
) as dag:
    # The collector retries pages itself (3 attempts); output also goes to
    # logs/collector.log, which the Pipeline console reads.
    collect = BashOperator(
        task_id="collect",
        bash_command=in_project(
            "mkdir -p logs; "
            "python src/airsense/ingestion/datagov_client.py 2>&1 | tee -a logs/collector.log"
        ),
        retries=1,
        retry_delay=timedelta(minutes=3),
        execution_timeout=timedelta(minutes=15),
    )
    # validate_processed is a gate: if the processed data fails its checks, no
    # new features reach Redis and the page keeps the last good values.
    (
        collect
        >> dvc_stage("process_live", timeout_min=5)
        >> dvc_stage("validate_processed", timeout_min=10)
        >> dvc_stage("build_features_live", timeout_min=5)
        >> dvc_stage("feast_online", timeout_min=5)
    )
