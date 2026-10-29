"""ForecastService logic with fake inputs: stale data, missing values, model
server down, negative forecasts, guidance failure (Stage 6)."""

import numpy as np
import pandas as pd
import pytest

from airsense.serving.service import (
    WEATHER_COLS,
    FeatureStoreBusy,
    FeatureStoreDown,
    ForecastService,
    ModelServerError,
)

T = pd.Timestamp("2026-10-23 10:30", tz="UTC")
FEATURES = ["pm25_24h", "pm25_lag_1h", "horizon", "temperature_2m_target", "city"]


class FakeModelServer:
    def __init__(self, change=1.0, fail=False):
        self.change, self.fail = change, fail

    def predict(self, rows):
        if self.fail:
            raise ModelServerError("connection refused")
        return np.full(len(rows), self.change)

    def ping(self):
        return not self.fail


class FakeBooster:
    def predict(self, rows, pred_contrib=False):
        return np.ones((len(rows), len(FEATURES) + 1))


def make_service(feats, t=T, change=1.0, fail=False):
    feats = {"pm25_lag_1h": 99.0, **feats}  # Feast returns every served feature
    s = ForecastService.__new__(ForecastService)  # skip loading Feast / MLflow
    s.params = {
        "validation": {"processed": {"max_live_age_hours": 4}},
        "features": {
            "rain_window_hours": 24,
            "local_timezone": "Asia/Kolkata",
            "festival_window_days": 2,
            "diwali_dates": ["2026-11-08"],
        },
        "forecast": {"horizon_hours": 24},
    }
    s.cities = {"Delhi": {"region": "North"}, "Pune": {"region": "West"}}
    s.cfg = {"top_factors": 2}
    s.codes = {"city": {"Delhi": 0}, "region": {"North": 0}}
    s.model = {"name": "m", "version": "1", "target_mode": "change"}
    s.features = FEATURES
    s.int_cols = set()
    s.model_server = FakeModelServer(change, fail)
    s.booster = FakeBooster()
    s.live_features = lambda city: (feats, t)
    hours = pd.date_range(T - pd.offsets.Hour(30), periods=60, freq="h")
    weather = pd.DataFrame({"city": "Delhi", "time_utc": hours})
    for c in [*WEATHER_COLS, "rain_24h"]:
        weather[c] = 1.0
    s.weather_table = lambda: (weather, None)
    s.guidance = lambda category, group: [{"category": category, "group": group}]
    return s


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    """forecast() asks pandas for the time; pin it 2 h after T."""
    real_now = pd.Timestamp.now
    monkeypatch.setattr(
        pd.Timestamp,
        "now",
        classmethod(lambda cls, tz=None: T + pd.offsets.Hour(2) if tz else real_now()),
    )


def test_normal_forecast():
    out = make_service({"pm25_24h": 100.0}).forecast("Delhi")
    assert out["available"] and len(out["forecast"]) == 24
    assert out["forecast"][0]["pm25"] == 101.0  # current + predicted change
    assert out["current"]["age_hours"] == 2.0 and out["warnings"] == []
    assert out["guidance"]["category"] == "Poor"


def test_stale_or_missing_current_value_gives_no_forecast():
    stale = make_service({"pm25_24h": 100.0}, t=T - pd.offsets.Hour(5))
    assert "h old" in stale.forecast("Delhi")["reason"]
    for value in (None, float("nan")):
        out = make_service({"pm25_24h": value}).forecast("Delhi")
        assert out["available"] is False and "no current" in out["reason"]


def test_city_unknown_to_model():
    out = make_service({"pm25_24h": 50.0}).forecast("Pune")
    assert out["available"] is False and "not known" in out["reason"]


def test_missing_history_is_reported_not_fatal():
    out = make_service({"pm25_24h": 100.0, "pm25_lag_1h": None}).forecast("Delhi")
    assert out["available"]
    assert out["warnings"] == ["PM2.5 history missing for: pm25_lag_1h"]


def test_negative_forecast_is_clipped_with_warning():
    out = make_service({"pm25_24h": 5.0}, change=-8.0).forecast("Delhi")
    assert all(h["pm25"] == 0 for h in out["forecast"])
    assert "negative forecasts set to 0" in out["warnings"]


def test_model_server_down_raises_for_the_api():
    with pytest.raises(ModelServerError):
        make_service({"pm25_24h": 100.0}, fail=True).forecast("Delhi")


def test_guidance_failure_keeps_the_forecast():
    s = make_service({"pm25_24h": 100.0})

    def broken(category, group):
        raise RuntimeError("storage folder is already accessed")

    s.guidance = broken
    out = s.forecast("Delhi")
    assert out["available"] and out["guidance"] is None
    assert out["warnings"][0].startswith("guidance unavailable")


def test_incomplete_weather_gives_no_forecast():
    s = make_service({"pm25_24h": 100.0})
    full, _ = s.weather_table()
    s.weather_table = lambda: (full.iloc[:35], "live weather forecast unavailable; ...")
    out = s.forecast("Delhi")
    assert out["available"] is False and "weather forecast missing" in out["reason"]
    assert out["warnings"] == ["live weather forecast unavailable; ..."]


def test_saved_forecast_is_used_when_live_weather_fails(tmp_path, monkeypatch):
    import airsense.ingestion.openmeteo_client as om

    day = tmp_path / "2026-10-23"
    day.mkdir()
    hours = pd.date_range("2026-10-22 00:00", periods=96, freq="h", tz="UTC")
    snap = pd.DataFrame({"city": "Delhi", "time_utc": hours})
    for c in om.HOURLY_VARS:
        snap[c] = 50.0
    snap["fetched_at_utc"] = "2026-10-23T12:30:00+00:00"
    snap.to_csv(day / "1230.csv", index=False)
    from airsense.serving import service

    monkeypatch.setattr(service, "SNAPSHOT_DIR", tmp_path)

    def offline(*args, **kwargs):
        raise RuntimeError("Open-Meteo request failed")

    monkeypatch.setattr(om, "fetch_weather", offline)
    s = make_service({"pm25_24h": 100.0})
    del s.weather_table  # use the real method
    s._weather, s._weather_at, s._weather_failed_at = None, 0.0, -1e9
    s._weather_lock = __import__("threading").Lock()
    s._weather_note = None
    s.cfg.update(
        weather_cache_minutes=30,
        weather_retry_minutes=5,
        weather_past_days=2,
        weather_forecast_days=3,
        weather_timeout_seconds=10,
    )
    s.params["features"].update(weather_offset_minutes=30)
    table, note = s.weather_table()
    assert len(table) == 96 and "fetched Fri 23 Oct 18:00 IST" in note


def test_model_server_error_keeps_mlflow_reason(monkeypatch):
    import httpx

    from airsense.serving.service import ModelServerClient

    class Answer:
        status_code = 400
        text = '{"message": "Failed to convert column relative_humidity_2m_now"}'

    monkeypatch.setattr(httpx, "post", lambda *a, **k: Answer())
    with pytest.raises(ModelServerError, match="relative_humidity_2m_now"):
        ModelServerClient("http://x").predict(pd.DataFrame({"a": [1.0]}))


def test_empty_integer_input_gives_no_forecast():
    """One empty value in an integer column would make the model server answer 400."""
    s = make_service({"pm25_24h": 100.0})
    full, _ = s.weather_table()
    holey = full.copy()
    holey.loc[holey.index[35], "temperature_2m"] = float("nan")  # one target hour
    s.weather_table = lambda: (holey, None)
    s.int_cols = {"temperature_2m_target"}
    out = s.forecast("Delhi")
    assert out["available"] is False and "temperature_2m_target" in out["reason"]
    s.int_cols = set()  # a decimal column may be empty: forecast is made
    assert s.forecast("Delhi")["available"] is True


def test_online_read_is_retried_once_then_reported(monkeypatch):
    from airsense.serving import service

    monkeypatch.setattr(service.time, "sleep", lambda s: None)
    s = ForecastService.__new__(ForecastService)
    answer = {"pm25_24h": [100.0], "pm25_24h__ts": [1792750000], "city": ["Delhi"]}
    calls = []

    def flaky(city):
        calls.append(city)
        if len(calls) == 1:
            raise IndexError("tuple index out of range")  # store being rewritten
        return answer

    s._read_online = flaky
    feats, t = s.live_features("Delhi")
    assert feats == {"pm25_24h": 100.0} and t is not None and len(calls) == 2

    def broken(city):
        raise IndexError("tuple index out of range")

    s._read_online = broken
    with pytest.raises(FeatureStoreBusy):
        s.live_features("Delhi")


def test_unreachable_redis_is_reported_as_down(monkeypatch):
    from redis.exceptions import ConnectionError as RedisConnectionError

    from airsense.serving import service

    monkeypatch.setattr(service.time, "sleep", lambda s: None)
    s = ForecastService.__new__(ForecastService)

    def down(city):
        raise RedisConnectionError("Error 111 connecting to redis:6379.")

    s._read_online = down
    with pytest.raises(FeatureStoreDown):
        s.live_features("Delhi")
