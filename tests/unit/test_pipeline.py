"""Pipeline console data from a small fake project (Stage 7)."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from airsense.serving.pipeline import (
    PipelineStatus,
    collection_health,
    edges,
    read_stages,
    recent_problems,
    stage_status,
    summarise_report,
    throughput,
    workflows,
)

DVC_YAML = """
stages:
  process:
    cmd: python process.py
    deps: [process.py, data/raw/live]
    outs: [data/processed/city]
    metrics:
      - data/interim/process_metrics.json:
          cache: false
  validate:
    cmd: python validate.py
    deps: [validate.py, data/processed/city]
    outs:
      - data/interim/validation.json:
          cache: false
    always_changed: true
  train:
    cmd: python train.py
    deps: [train.py, data/processed]
    outs: [models/model.txt]
"""


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "dvc.yaml").write_text(DVC_YAML)
    interim = tmp_path / "data" / "interim"
    interim.mkdir(parents=True)
    (interim / "process_metrics.json").write_text(
        json.dumps(
            {
                "built_at_utc": "2026-10-24T09:00:00+00:00",
                "rows": 6000,
                "seconds": 2.0,
                "columns": ["a"] * 50,
            }
        )
    )
    (interim / "validation.json").write_text(
        json.dumps(
            {
                "checked_at_utc": "2026-10-24T09:01:00+00:00",
                "critical_passed": True,
                "warnings": ["Pune coverage 0.83"],
            }
        )
    )
    (interim / "gate_report.json").write_text(
        json.dumps({"passed": True, "checks": []})
    )
    (interim / "registry.json").write_text(json.dumps({"champion_version": 1}))
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "model.txt").write_text("tree")
    day = tmp_path / "data" / "raw" / "live" / "datagov" / "2026-10-24"
    day.mkdir(parents=True)
    for hhmm in ["0705", "0735", "0805", "0935", "1005"]:  # gap 08:05 -> 09:35
        (day / f"{hhmm}.csv").write_text("x")
    (day / "notes.csv").write_text("ignored: not a time")
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "collector.log").write_text(
        "2026-10-24 09:05:01,100 INFO collector: 520 records\n"
        "2026-10-24 09:35:02,200 WARNING collector: partial snapshot (3 records)\n"
        "2026-10-24 10:05:03,300 ERROR collector: HTTP 502 after 3 attempts\n"
        "Traceback (most recent call last):\n"
    )
    return tmp_path


def test_stages_reports_and_graph(project):
    stages = read_stages(project / "dvc.yaml")
    assert [s["name"] for s in stages] == ["process", "validate", "train"]
    assert stages[0]["report"] == "data/interim/process_metrics.json"
    assert stages[2]["report"] is None
    # data/processed/city feeds validate; train reads the parent folder data/processed
    assert edges(stages) == [["process", "validate"], ["process", "train"]]


def test_stage_status_from_dvc():
    status = {
        "train": [{"changed deps": {"train.py": "modified"}}],
        "validate": ["always changed"],
    }
    assert stage_status("train", False, status) == (
        "changed",
        ["changed deps: train.py (modified)"],
    )
    assert stage_status("validate", True, status) == ("runs every time", [])
    assert stage_status("process", False, status) == ("up to date", [])
    assert stage_status("process", False, None) == ("unknown", [])


def test_report_summary_and_throughput():
    report = {"rows": 6000, "seconds": 2.0, "columns": ["a"] * 50, "nested": {"x": 1}}
    assert summarise_report(report) == {"rows": 6000, "seconds": 2.0}
    assert throughput(report) == {"rows": 6000, "seconds": 2.0, "rows_per_second": 3000}
    assert (
        throughput({"rows_per_second": 60181, "rows": 1, "seconds": 9})[
            "rows_per_second"
        ]
        == 60181
    )
    assert throughput({"passed": True}) is None
    # other report layouts: count named raw_records, time named seconds_total
    assert throughput(
        {"raw_records": 45360, "seconds_total": 0.27, "repeats_removed": 9}
    ) == {"rows": 45360, "seconds": 0.27, "rows_per_second": 168000}


def test_collection_health_finds_gaps():
    t0 = datetime(2026, 10, 24, 7, 5, tzinfo=UTC)
    times = [t0 + timedelta(minutes=m) for m in (0, 30, 60, 150, 180)]
    h = collection_health(times, now=t0 + timedelta(minutes=200))
    assert h["snapshots_24h"] == 5 and h["expected_24h"] == 48
    assert h["minutes_since_last"] == 20
    # the window starts 24 h before now (1240 min before the first snapshot)
    assert [g["minutes"] for g in h["gaps_24h"]] == [1240, 90]
    assert not any(g["ongoing"] for g in h["gaps_24h"])
    assert len(h["times_24h"]) == 5 and h["times_24h"][0] == t0.isoformat()
    empty = collection_health([], now=t0)
    assert empty["last_snapshot_utc"] is None
    # collector stopped: one ongoing 24-hour gap, never "no gaps"
    assert [(g["minutes"], g["ongoing"]) for g in empty["gaps_24h"]] == [(1440, True)]


def test_recent_problems_newest_first(project):
    found = recent_problems(project / "logs")
    assert [f["level"] for f in found] == ["ERROR", "WARNING"]
    assert found[0]["message"] == "collector: HTTP 502 after 3 attempts"


def test_full_build_and_dvc_failure(project):
    status = {"train": [{"changed deps": {"train.py": "modified"}}]}
    p = PipelineStatus(
        root=project, status_fn=lambda root, t: status, model_info={"version": "1"}
    )
    out = p.get()
    by_name = {s["name"]: s for s in out["stages"]}
    assert by_name["process"]["status"] == "up to date"
    assert by_name["process"]["last_run_utc"] == "2026-10-24T09:00:00+00:00"
    assert by_name["train"]["status"] == "changed"
    assert by_name["train"]["last_run_utc"] is not None  # from the model file's time
    assert out["throughput"] == [
        {"stage": "process", "rows": 6000, "seconds": 2.0, "rows_per_second": 3000}
    ]
    assert out["collection"]["total_snapshots"] == 5
    assert out["gate"]["passed"] and out["registry"]["champion_version"] == 1
    assert out["champion"] == {"version": "1"}

    def broken(root, timeout):
        raise RuntimeError("ERROR: Unable to acquire lock")

    out = PipelineStatus(root=project, status_fn=broken).build()
    assert out["dvc_status_error"].startswith("ERROR")
    assert {s["status"] for s in out["stages"]} == {"unknown"}


def test_cache_avoids_rebuilding(project):
    calls = []

    def counting(root, timeout):
        calls.append(1)
        return {}

    p = PipelineStatus(root=project, status_fn=counting, cache_seconds=60)
    p.get()
    p.get()
    assert len(calls) == 1


def test_airflow_workflows_from_runs_file(project):
    now = datetime(2026, 10, 31, 6, 0, tzinfo=UTC)
    runs = {
        "airsense_retrain": {
            "state": "success",
            "start_date": "2026-10-31T04:52:30+00:00",
            "end_date": "2026-10-31T05:00:00+00:00",
        },
        "airsense_live": {
            "state": "failed",
            "run_type": "scheduled",
            "end_date": "2026-10-31T05:40:00+00:00",
        },
    }
    out = workflows(runs, now, "http://localhost:8081/")
    assert [w["dag_id"] for w in out] == ["airsense_live", "airsense_retrain"]
    assert out[0]["state"] == "failed" and out[0]["minutes_ago"] == 20
    assert out[0]["url"] == "http://localhost:8081/dags/airsense_live"
    assert out[1]["duration_min"] == 7.5 and out[0]["duration_min"] is None
    assert workflows(None, now, None) == []  # Airflow not set up: nothing shown
    assert workflows({"x": {"state": "running"}}, now, None)[0]["minutes_ago"] is None

    (project / "logs").mkdir(exist_ok=True)
    (project / "logs" / "airflow_runs.json").write_text(json.dumps(runs))
    p = PipelineStatus(root=project, status_fn=lambda r, t: {}, airflow_url="http://a")
    built = p.build()
    assert {w["dag_id"] for w in built["workflows"]} == set(runs)
    assert built["airflow_url"] == "http://a"
