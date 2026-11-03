"""The monitor's retrain decision (Stage 10)."""

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "airflow" / "dags"))
from monitor_decision import decide, decide_and_record

NOW = datetime(2026, 11, 5, 17, 20, tzinfo=UTC)
CFG = {"auto_retrain": True, "retrain_cooldown_days": 3}
HIGH = {
    "error_high": True,
    "limit_mae": 10.2,
    "overall": {"mae_model": 12.5, "mae_no_change": 11.0},
}
DRIFT = {
    "drift": True,
    "checked_at_utc": NOW.isoformat(),
    "inputs": {"pm25_24h": {"level": "drift"}, "rain_24h": {"level": "stable"}},
}


def test_reasons_and_no_reason():
    assert decide(HIGH, None, CFG, None, NOW)[0] is True
    start, why = decide(None, DRIFT, CFG, None, NOW)
    assert start is True and why == "data drift in pm25_24h"
    assert decide({"error_high": False}, {"drift": False}, CFG, None, NOW)[0] is False


def test_old_drift_report_is_ignored():
    old = {**DRIFT, "checked_at_utc": (NOW - timedelta(hours=30)).isoformat()}
    assert decide(None, old, CFG, None, NOW)[0] is False


def test_switch_and_cooldown():
    start, why = decide(HIGH, None, {**CFG, "auto_retrain": False}, None, NOW)
    assert start is False and "automatic retraining is off" in why
    recent = {"triggered_at_utc": (NOW - timedelta(days=2)).isoformat()}
    assert decide(HIGH, None, CFG, recent, NOW)[0] is False
    old = {"triggered_at_utc": (NOW - timedelta(days=4)).isoformat()}
    assert decide(HIGH, None, CFG, old, NOW)[0] is True


def test_decision_is_recorded_and_starts_the_cooldown(tmp_path):
    rep = tmp_path / "data" / "monitoring" / "reports"
    rep.mkdir(parents=True)
    (rep / "evaluation.json").write_text(json.dumps(HIGH))
    assert decide_and_record(tmp_path, CFG, NOW) is True
    assert (
        decide_and_record(tmp_path, CFG, NOW + timedelta(hours=1)) is False
    )  # cooldown
    d = json.loads((rep / "retrain_decision.json").read_text())
    assert d["retrain"] is False and "cooldown" in d["reason"]
