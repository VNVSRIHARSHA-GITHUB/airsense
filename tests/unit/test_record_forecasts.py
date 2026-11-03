"""Saving forecasts for the forecast-vs-actual check (Stage 10)."""

from datetime import UTC, datetime

import httpx
import pandas as pd

from airsense.monitoring.record_forecasts import record, rows_from

NOW = datetime(2026, 11, 3, 12, 50, tzinfo=UTC)


def forecast(city, available=True):
    if not available:
        return {
            "city": city,
            "available": False,
            "reason": "old",
            "model": {"version": 1},
        }
    return {
        "city": city,
        "available": True,
        "model": {"version": 1},
        "current": {"pm25_24h": 200.0, "time_utc": "2026-11-03T10:30:00+00:00"},
        "forecast": [
            {
                "horizon": h,
                "time_utc": f"2026-11-03T{10 + h}:30:00+00:00",
                "pm25": 200.0 + h,
            }
            for h in (1, 2)
        ],
    }


def fake_backend(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/cities":
        return httpx.Response(
            200, json=[{"city": c} for c in ("Delhi", "Guwahati", "Pune")]
        )
    city = path.rsplit("/", 1)[1]
    if city == "Pune":
        return httpx.Response(503, json={"detail": "Model server unavailable"})
    return httpx.Response(200, json=forecast(city, available=city == "Delhi"))


def test_rows_keep_the_start_value_as_the_no_change_forecast():
    rows = rows_from(forecast("Delhi"), NOW)
    assert [r["horizon"] for r in rows] == [1, 2]
    assert rows[1]["base_pm25"] == 200.0 and rows[1]["pm25_forecast"] == 202.0
    assert rows[1]["target_time_utc"] == pd.Timestamp("2026-11-03T12:30:00+00:00")
    assert rows_from(forecast("Guwahati", available=False), NOW) == []


def test_record_saves_one_file_and_reports_the_rest(tmp_path):
    client = httpx.Client(
        transport=httpx.MockTransport(fake_backend), base_url="http://b"
    )
    s = record(client, out_dir=tmp_path, now=NOW)
    assert (
        s["saved"] == 1 and s["unavailable"] == ["Guwahati"] and s["failed"] == ["Pune"]
    )
    saved = pd.read_parquet(tmp_path / "2026-11-03" / "1250.parquet")
    assert len(saved) == 2 and set(saved["city"]) == {"Delhi"}
    assert str(saved["target_time_utc"].dt.tz) == "UTC"
