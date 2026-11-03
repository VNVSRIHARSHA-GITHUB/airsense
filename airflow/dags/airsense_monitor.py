"""airsense_monitor: is the model still good? (Stage 10)

record_forecasts: every hour, the forecast of every city is saved
(data/monitoring/forecasts/), exactly as the backend serves it, so that 24 hours
later it can be compared with what was actually measured.

(The comparison, the daily drift check and the automatic retrain trigger are
added to this DAG in the next steps of Stage 10.)

Hourly at :50, after the :35 live run has refreshed the features.
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

from airflow.providers.standard.operators.bash import BashOperator
from airflow.sdk import DAG

sys.path.insert(0, str(Path(__file__).parent))  # the shared helper next to this file
from airsense_common import in_project, record_run

with DAG(
    dag_id="airsense_monitor",
    description="Save every city's forecast hourly for the forecast-vs-actual check",
    schedule="50 * * * *",
    start_date=datetime(2026, 11, 3),
    catchup=False,  # a forecast can only be saved now, not for a past hour
    max_active_runs=1,
    dagrun_timeout=timedelta(minutes=20),
    tags=["airsense", "monitoring"],
    doc_md=__doc__,
    on_success_callback=record_run,  # last run for the Pipeline console
    on_failure_callback=record_run,
) as dag:
    # Not in the dvc pool: it only calls the backend's API and writes its own folder.
    record_forecasts = BashOperator(
        task_id="record_forecasts",
        bash_command=in_project("python -m airsense.monitoring.record_forecasts"),
        retries=1,
        retry_delay=timedelta(minutes=2),
        execution_timeout=timedelta(minutes=10),
    )
