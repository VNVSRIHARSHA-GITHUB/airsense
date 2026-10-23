"""ForecastService logic with fake inputs: stale data, missing values, model
server down, negative forecasts, guidance failure (Stage 6)."""

import numpy as np
import pandas as pd
import pytest

from airsense.serving.service import WEATHER_COLS, ForecastService, ModelServerError

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
