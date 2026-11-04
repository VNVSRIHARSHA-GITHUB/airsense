"""airsense_monitor: is the model still good? (Stage 10)

record_forecasts -> evaluate -> decide_retrain -> trigger_retrain
                           \\-> drift_due -> check_drift -/

- record_forecasts: every city's forecast, as the backend serves it, saved to
  data/monitoring/forecasts/ for the check 24 hours later.
- evaluate: saved forecasts whose hour has passed vs the measured values, model
  and "no change" (data/monitoring/reports/evaluation.json).
- drift_due / check_drift: once a day (the 22:50 IST run, or any manual run),
  PSI of the live inputs vs training data of the same months (drift.json).
- decide_retrain / trigger_retrain: start airsense_retrain when the error is high
  or there is drift, if automatic retraining is on, at most every few days
  (monitor_decision.py). The quality gate still decides about the new model.

evaluate and check_drift read files the live pipeline rewrites every 30 min, so
they use the `dvc` pool and never run while a DVC step writes them. evaluate runs
even if record_forecasts failed (backend down): the earlier saved forecasts can
still be checked, and the drift check and the retrain decision go on. Airflow
judges the run by its last tasks, so such a run may end green with a red
record_forecasts box; the outage itself is reported by the BackendDown alert.
Hourly at :50, after the :35 live run.
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from airflow.providers.standard.operators.bash import BashOperator
from airflow.providers.standard.operators.python import ShortCircuitOperator
from airflow.providers.standard.operators.trigger_dagrun import TriggerDagRunOperator
from airflow.sdk import DAG

sys.path.insert(0, str(Path(__file__).parent))  # the shared helpers next to this file
from airsense_common import DVC_POOL, PROJECT, in_project, record_run
from monitor_decision import decide_and_record


def monitoring_params() -> dict:
    with open(Path(PROJECT) / "params.yaml") as f:
        return yaml.safe_load(f)["monitoring"]


def drift_is_due(**context) -> bool:
    """The daily drift check: the run of drift_hour_ist, or any manual run."""
    run = context["dag_run"]
    if str(getattr(run.run_type, "value", run.run_type)) == "manual":
        return True
    hour = datetime.now(ZoneInfo("Asia/Kolkata")).hour
    return hour == monitoring_params()["drift_hour_ist"]


def retrain_needed() -> bool:
    return decide_and_record(Path(PROJECT), monitoring_params())


with DAG(
    dag_id="airsense_monitor",
    description="Forecasts vs actuals, data drift, automatic retrain trigger",
    schedule="50 * * * *",
    start_date=datetime(2026, 11, 3),
    catchup=False,  # a forecast can only be saved now, not for a past hour
    max_active_runs=1,
    dagrun_timeout=timedelta(minutes=30),
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
    evaluate = BashOperator(
        task_id="evaluate",
        bash_command=in_project("python -m airsense.monitoring.evaluate"),
        trigger_rule="all_done",  # also when record_forecasts failed (backend down)
        pool=DVC_POOL,
        retries=1,
        retry_delay=timedelta(minutes=2),
        execution_timeout=timedelta(minutes=10),
    )
    drift_due = ShortCircuitOperator(
        task_id="drift_due",
        python_callable=drift_is_due,
        ignore_downstream_trigger_rules=False,  # skip only check_drift, not the decision
    )
    check_drift = BashOperator(
        task_id="check_drift",
        bash_command=in_project("python -m airsense.monitoring.drift"),
        pool=DVC_POOL,
        execution_timeout=timedelta(minutes=10),
    )
    decide_retrain = ShortCircuitOperator(
        task_id="decide_retrain",
        python_callable=retrain_needed,
        trigger_rule="none_failed_min_one_success",  # also when check_drift was skipped
    )
    trigger_retrain = TriggerDagRunOperator(
        task_id="trigger_retrain",
        trigger_dag_id="airsense_retrain",
        wait_for_completion=False,  # the retrain is followed in its own DAG
    )
    record_forecasts >> evaluate >> [drift_due, decide_retrain]
    drift_due >> check_drift >> decide_retrain >> trigger_retrain
