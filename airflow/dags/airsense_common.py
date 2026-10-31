"""Shared helpers for the AirSense DAGs (Stage 9). Not a DAG itself.

Every pipeline step runs as a shell command in the project folder with the
project's own environment (.venv) activated, so `python`, `dvc` and `feast` in
dvc.yaml's commands are the pipeline's versions, not Airflow's.
"""

import fcntl
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from airflow.providers.standard.operators.bash import BashOperator

# The project folder: set by airflow/start.sh; otherwise two levels above this file.
PROJECT = os.environ.get("AIRSENSE_PROJECT") or str(Path(__file__).resolve().parents[2])

# DVC allows one command per repository at a time (it locks the repo), so every
# DVC task in every DAG uses this pool with a single slot: they queue instead of
# failing with "Unable to acquire lock".
DVC_POOL = "dvc"

# The last run of each DAG, for the Pipeline console (read by the backend, which
# sees the project folder; no Airflow login or network access needed).
RUNS_FILE = Path(PROJECT) / "logs" / "airflow_runs.json"


def _iso(t) -> str | None:
    return t.isoformat() if t else None


def record_run(context) -> None:
    """DAG callback (success and failure): write this run's outcome to RUNS_FILE."""
    run = context["dag_run"]
    state = getattr(run.state, "value", run.state)
    entry = {
        "state": str(state),
        "run_id": run.run_id,
        "run_type": str(getattr(run.run_type, "value", run.run_type)),
        "logical_date": _iso(getattr(run, "logical_date", None)),
        "start_date": _iso(run.start_date),
        "end_date": _iso(run.end_date) or datetime.now(UTC).isoformat(),
        "reason": context.get("reason"),
    }
    RUNS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(RUNS_FILE.with_suffix(".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # DAGs may finish at the same moment
        try:
            runs = json.loads(RUNS_FILE.read_text())
        except (OSError, ValueError):
            runs = {}
        runs[run.dag_id] = entry
        tmp = RUNS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(runs, indent=2))
        tmp.replace(RUNS_FILE)  # readers never see a half-written file


def in_project(command: str) -> str:
    """A bash command run in the project folder with .venv activated."""
    return f'set -euo pipefail; cd "{PROJECT}"; source .venv/bin/activate; {command}'


def dvc_stage(stage: str, timeout_min: int = 15, retries: int = 1) -> BashOperator:
    """One DVC stage as one task. `-s` (single item): only this stage; the order
    between stages is the DAG's job, so each step is visible in the graph."""
    return BashOperator(
        task_id=stage,
        bash_command=in_project(f"dvc repro -s {stage}"),
        pool=DVC_POOL,
        retries=retries,
        retry_delay=timedelta(minutes=2),
        execution_timeout=timedelta(minutes=timeout_min),
    )
