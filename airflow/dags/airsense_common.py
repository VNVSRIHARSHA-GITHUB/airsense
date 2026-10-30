"""Shared helpers for the AirSense DAGs (Stage 9). Not a DAG itself.

Every pipeline step runs as a shell command in the project folder with the
project's own environment (.venv) activated, so `python`, `dvc` and `feast` in
dvc.yaml's commands are the pipeline's versions, not Airflow's.
"""

import os
from datetime import timedelta
from pathlib import Path

from airflow.providers.standard.operators.bash import BashOperator

# The project folder: set by airflow/start.sh; otherwise two levels above this file.
PROJECT = os.environ.get("AIRSENSE_PROJECT") or str(Path(__file__).resolve().parents[2])

# DVC allows one command per repository at a time (it locks the repo), so every
# DVC task in every DAG uses this pool with a single slot: they queue instead of
# failing with "Unable to acquire lock".
DVC_POOL = "dvc"


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
