"""Prometheus metrics of the backend: request counts, forecasts, read-on-scrape
values, collector status (Stage 10)."""

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
from main import create_app
from test_api import FakeService


def metric_lines(client) -> list[str]:
    r = client.get("/metrics")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    return [line for line in r.text.splitlines() if line and not line.startswith("#")]


def value(lines, prefix) -> float:
    found = [float(line.rsplit(" ", 1)[1]) for line in lines if line.startswith(prefix)]
    assert found, f"no metric line starting with {prefix}"
    return found[0]


def test_requests_and_forecasts_are_counted(tmp_path):
    client = TestClient(create_app(service=FakeService(), root=tmp_path))
    client.get("/forecast/Delhi")
    client.get("/forecast/Delhi")
    client.get("/forecast/Delhi?group=sensitive")  # fake: no forecast possible
    client.get("/forecast/Delhi?group=outdoor_workers")  # fake: model server down
    client.get("/forecast/Atlantis")  # 404
    lines = metric_lines(client)
    req = (
        'airsense_http_requests_total{endpoint="/forecast/{city}",method="GET",status='
    )
    assert value(lines, req + '"200"}') == 3  # route template, not 3 different URLs
    assert value(lines, req + '"503"}') == 1
    assert value(lines, req + '"404"}') == 1
    assert value(lines, 'airsense_forecasts_total{city="Delhi",result="made"}') == 2
    assert (
        value(lines, 'airsense_forecasts_total{city="Delhi",result="unavailable"}') == 1
    )
    assert value(lines, 'airsense_forecasts_total{city="Delhi",result="error"}') == 1
    assert not any("/metrics" in line for line in lines)  # scrapes are not counted


def test_values_read_on_scrape(tmp_path):
    snap = tmp_path / "data" / "raw" / "live" / "datagov"
    now = datetime.now(UTC)
    for minutes in (95, 65, 35):
        t = now - timedelta(minutes=minutes)
        (snap / f"{t:%Y-%m-%d}").mkdir(parents=True, exist_ok=True)
        (snap / f"{t:%Y-%m-%d}" / f"{t:%H%M}.csv").write_text("x\n")
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "collector_status.json").write_text(
        json.dumps(
            {
                "time_utc": now.isoformat(),
                "result": "empty",
                "records": 0,
                "last_ok_utc": (now - timedelta(minutes=35)).isoformat(),
            }
        )
    )
    lines = metric_lines(TestClient(create_app(service=FakeService(), root=tmp_path)))
    assert value(lines, 'airsense_dependency_up{dependency="model_server"}') == 1
    assert value(lines, 'airsense_data_age_hours{city="Delhi"}') == 2.4
    assert value(lines, 'airsense_city_has_data{city="Delhi"}') == 1
    assert value(lines, "airsense_champion_version") == 1
    assert value(lines, "airsense_snapshots_last_24h") == 3
    assert 34 <= value(lines, "airsense_minutes_since_last_snapshot") <= 36
    assert value(lines, 'airsense_collector_last_result{result="empty"}') == 1
    assert value(lines, 'airsense_collector_last_result{result="ok"}') == 0


def test_scrape_works_without_collector_status_or_snapshots(tmp_path):
    lines = metric_lines(TestClient(create_app(service=FakeService(), root=tmp_path)))
    assert value(lines, "airsense_snapshots_last_24h") == 0
    assert not any(line.startswith("airsense_collector_last_result") for line in lines)


def test_monitoring_reports_become_metrics(tmp_path):
    rep = tmp_path / "data" / "monitoring" / "reports"
    rep.mkdir(parents=True)
    (rep / "evaluation.json").write_text(
        json.dumps(
            {
                "evaluated_at_utc": "2026-11-05T12:00:00+00:00",
                "limit_mae": 10.2,
                "error_high": True,
                "overall": {
                    "pairs": 300,
                    "mae_model": 12.5,
                    "mae_no_change": 11.0,
                    "skill": -0.136,
                },
                "by_horizon": {
                    "1-6 h": {"mae_model": 5.0},
                    "13-24 h": {"mae_model": None},
                },
            }
        )
    )
    (rep / "drift.json").write_text(
        json.dumps(
            {
                "checked_at_utc": "2026-11-05T17:20:00+00:00",
                "inputs": {
                    "pm25_24h": {"psi": 0.45, "level": "drift"},
                    "rain_24h": {"psi": None, "level": "not enough data"},
                },
            }
        )
    )
    lines = metric_lines(TestClient(create_app(service=FakeService(), root=tmp_path)))
    assert value(lines, 'airsense_forecast_mae{forecast="model"}') == 12.5
    assert value(lines, 'airsense_forecast_mae{forecast="no_change"}') == 11.0
    assert value(lines, "airsense_forecast_error_high") == 1
    assert value(lines, 'airsense_forecast_mae_by_horizon{hours_ahead="1-6 h"}') == 5.0
    assert value(lines, 'airsense_drift_psi{input="pm25_24h"}') == 0.45
    assert value(lines, 'airsense_drift_level{input="pm25_24h"}') == 2
    assert not any('input="rain_24h"' in line for line in lines)  # no PSI: no value
