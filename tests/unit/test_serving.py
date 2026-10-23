"""Unit tests for serving: training/serving consistency, explanations (Stage 6)."""

import numpy as np
import pandas as pd

from airsense.features.build_training import expand_horizon
from airsense.serving.assemble import aqi_name, build_rows
from airsense.serving.explain import label, top_factors
from airsense.training.train import encode_categories, feature_columns

CAL = {
    "local_timezone": "Asia/Kolkata",
    "festival_window_days": 2,
    "diwali_dates": ["2023-11-12"],
}
WCOLS = ["temperature_2m", "rain_24h"]
CODES = {"city": {"Delhi": 0, "Pune": 1}, "region": {"North": 0, "West": 1}}


def tables(n=60):
    """A small Feast-like base table and the matching weather table."""
    rng = np.random.default_rng(0)
    t = pd.date_range("2023-11-10 00:30", periods=n, freq="h", tz="UTC")
    weather = pd.DataFrame(
        {
            "city": "Delhi",
            "time_utc": t,
            "temperature_2m": rng.uniform(10, 30, n),
            "rain_24h": rng.uniform(0, 5, n),
        }
    )
    base = weather.assign(
        pm25_24h=rng.uniform(50, 300, n),
        pm25_lag_1h=rng.uniform(50, 300, n),
        n_stations_24h=3.0,
    )
    return base, weather


def test_serving_rows_equal_training_rows():
    """The key consistency check: same inputs -> identical model rows."""
    base, weather = tables()
    t = base["time_utc"].iloc[10]
    expected = pd.concat(
        [expand_horizon(base, h, WCOLS, {"Delhi": "North"}, CAL) for h in range(1, 25)]
    )
    expected = expected[expected["time_utc"] == t].sort_values("horizon")
    expected = encode_categories(expected.reset_index(drop=True), CODES)
    order = feature_columns(expected)

    pm = base.loc[base["time_utc"] == t, ["pm25_24h", "pm25_lag_1h", "n_stations_24h"]]
    rows, target_times, missing = build_rows(
        pm=pm.iloc[0].to_dict(),
        t=t,
        weather=weather,
        weather_cols=WCOLS,
        city_code=0,
        region_code=0,
        cal_cfg=CAL,
        feature_order=order,
    )
    assert missing == 0
    assert list(target_times) == list(expected["target_time_utc"])
    pd.testing.assert_frame_equal(
        rows.astype(float), expected[order].astype(float).reset_index(drop=True)
    )


def test_missing_weather_is_counted_and_left_empty():
    base, weather = tables(n=20)  # too short for t + 24
    t = base["time_utc"].iloc[5]
    rows, _, missing = build_rows(
        pm={"pm25_24h": 100.0},
        t=t,
        weather=weather,
        weather_cols=WCOLS,
        city_code=0,
        region_code=0,
        cal_cfg=CAL,
        feature_order=["pm25_24h", "temperature_2m_target", "horizon"],
    )
    assert len(rows) == 24 and missing == 10  # hours 15..24 after t lie beyond the data
    assert rows["temperature_2m_target"].isna().sum() == 10


def test_aqi_names():
    assert [aqi_name(v) for v in (10, 30, 45, 100, 200, 300)] == [
        "Good",
        "Good",
        "Satisfactory",
        "Poor",
        "Very Poor",
        "Severe",
    ]
    assert aqi_name(float("nan")) is None


def test_labels_and_top_factors():
    assert label("wind_speed_10m_target") == "wind speed expected"
    assert label("pm25_lag_24h") == "PM2.5 24h ago"
    features = ["pm25_24h", "wind_dir_sin_target", "wind_dir_cos_target", "hour"]
    contrib = np.array([[10.0, -3.0, -4.0, 1.0, 50.0], [20.0, -3.0, -4.0, -1.0, 50.0]])
    top = top_factors(contrib, features, n=2)
    assert [f["factor"] for f in top] == [
        "current PM2.5 level",
        "wind direction expected",
    ]
    assert top[1] == {
        "factor": "wind direction expected",
        "effect_ug_m3": -7.0,
        "direction": "lowers",
    }


def test_missing_pm_feature_becomes_nan():
    """Feast gives None for an empty feature; it must reach the model as NaN."""
    _, weather = tables()
    rows, _, _ = build_rows(
        pm={"pm25_24h": 100.0, "pm25_lag_72h": None},
        t=weather["time_utc"].iloc[5],
        weather=weather,
        weather_cols=WCOLS,
        city_code=0,
        region_code=0,
        cal_cfg=CAL,
        feature_order=["pm25_24h", "pm25_lag_72h"],
    )
    assert rows["pm25_lag_72h"].isna().all() and (rows["pm25_24h"] == 100).all()
