"""Alerts from Alertmanager kept for the console (Stage 10)."""

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from airsense.monitoring.alerts import ALERTS_FILE, AlertStore, alerts_summary

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
from main import create_app
from test_api import FakeService

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


def alert(name, status="firing", severity="warning", fp=None):
    return {
        "status": status,
        "fingerprint": fp or name,
        "labels": {"alertname": name, "severity": severity},
        "annotations": {"summary": f"{name} summary", "description": "d"},
        "startsAt": "2026-11-03T11:50:00Z",
        "endsAt": "2026-11-03T12:00:00Z"
        if status == "resolved"
        else "0001-01-01T00:00:00Z",
    }


def test_firing_then_resolved(tmp_path):
    store = AlertStore(tmp_path / "alerts.json")
    store.receive(
        {
            "status": "firing",
            "alerts": [
                alert("LiveDataStale"),
                alert("BackendDown", severity="critical"),
            ],
        },
        NOW,
    )
    s = alerts_summary(tmp_path / "alerts.json")
    assert [a["alertname"] for a in s["firing"]] == [
        "BackendDown",
        "LiveDataStale",
    ]  # critical first
    assert s["firing"][0]["ends_at"] is None  # Alertmanager's "year 1" is not kept
    store.receive(
        {"status": "resolved", "alerts": [alert("BackendDown", "resolved")]}, NOW
    )
    s = alerts_summary(tmp_path / "alerts.json")
    assert [a["alertname"] for a in s["firing"]] == ["LiveDataStale"]
    assert s["resolved"][0]["alertname"] == "BackendDown"


def test_old_resolved_alerts_are_dropped(tmp_path):
    store = AlertStore(tmp_path / "alerts.json")
    store.receive({"alerts": [alert("A", "resolved")]}, NOW - timedelta(days=8))
    store.receive({"alerts": [alert("B", "resolved")]}, NOW)
    names = [
        a["alertname"] for a in alerts_summary(tmp_path / "alerts.json")["resolved"]
    ]
    assert names == ["B"]


def test_webhook_endpoint_stores_alerts(tmp_path):
    client = TestClient(create_app(service=FakeService(), root=tmp_path))
    r = client.post(
        "/alerts", json={"status": "firing", "alerts": [alert("CPCBFeedEmpty")]}
    )
    assert r.status_code == 200 and r.json() == {"stored": 1}
    assert (
        alerts_summary(tmp_path / ALERTS_FILE)["firing"][0]["alertname"]
        == "CPCBFeedEmpty"
    )
    assert alerts_summary(tmp_path / "missing.json") == {"firing": [], "resolved": []}
