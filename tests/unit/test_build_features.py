"""Unit tests for the shared feature functions (Stage 3.4)."""

import numpy as np
import pandas as pd
import pytest
from airsense.features.build_features import (
    calendar_features,
    complete_hourly_grid,
    for_storage,
    pm_features,
    weather_features,
)

CFG = {
    "lags_hours": [1, 24],
    "change_hours": [1],
    "rolling_hours": 3,
    "rolling_min_hours": 2,
    "rain_window_hours": 3,
    "weather_offset_minutes": 30,
    "local_timezone": "Asia/Kolkata",
    "festival_window_days": 2,
    "diwali_dates": ["2023-11-12"],
}


def pm_table(values, city="Delhi", start="2023-01-01 00:30"):
    """City-hour PM2.5 rows (labelled at :30 UTC, like the real data)."""
    return pd.DataFrame(
        {
            "city": city,
            "time_utc": pd.date_range(start, periods=len(values), freq="h"),
            "pm25_24h": [float(v) for v in values],
            "n_stations_24h": 3,
        }
    )


# ---------- hourly grid and lags ----------


def test_grid_adds_missing_hours():
    df = pm_table(range(5)).drop(index=2)  # hour 02:30 missing
    out = complete_hourly_grid(df)
    assert len(out) == 5
    assert np.isnan(out["pm25_24h"].iloc[2])


def test_lag_across_a_gap_is_empty_not_the_previous_row():
    df = pm_table([10, 20, 30, 40]).drop(index=2)  # 02:30 missing
    out = pm_features(df, CFG).set_index("time_utc")
    # lag 1 at 03:30 is the 02:30 value (missing), not 20 (the previous row)
    assert np.isnan(out.loc["2023-01-01 03:30", "pm25_lag_1h"])
    assert out.loc["2023-01-01 01:30", "pm25_lag_1h"] == 10


def test_lag_24_is_24_hours_back():
    out = pm_features(pm_table(range(30)), CFG)
    assert out["pm25_lag_24h"].iloc[24] == 0  # value of hour 0
    assert np.isnan(out["pm25_lag_24h"].iloc[23])


def test_cities_do_not_leak_into_each_other():
    df = pd.concat([pm_table([1, 2, 3], "Delhi"), pm_table([7, 8, 9], "Pune")])
    out = pm_features(df, CFG).set_index(["city", "time_utc"])
    assert np.isnan(out.loc[("Pune", pd.Timestamp("2023-01-01 00:30")), "pm25_lag_1h"])


def test_change_and_rolling_values():
    out = pm_features(pm_table([10, 20, 60]), CFG)
    assert out["pm25_change_1h"].tolist()[1:] == [10.0, 40.0]
    assert np.isnan(out["pm25_mean_3h"].iloc[0])  # 1 value < rolling_min_hours
    assert out["pm25_mean_3h"].iloc[1] == 15.0
    assert out["pm25_mean_3h"].iloc[2] == 30.0
    assert out["pm25_max_3h"].iloc[2] == 60.0


def test_features_never_use_future_values():
    """Leakage test: changing the last value must not change any earlier feature."""
    a = pm_features(pm_table(range(1, 31)), CFG)
    b = pm_features(pm_table([*range(1, 30), 999]), CFG)
    cols = [c for c in a.columns if c not in ("city", "time_utc")]
    pd.testing.assert_frame_equal(a[cols].iloc[:-1], b[cols].iloc[:-1])


# ---------- weather ----------


def weather_table(n=5, direction=0.0):
    return pd.DataFrame(
        {
            "city": "Delhi",
            "time_utc": pd.date_range("2023-01-01 00:00", periods=n, freq="h"),
            "temperature_2m": 20.0,
            "relative_humidity_2m": 50.0,
            "precipitation": [1.0, 0.0, 2.0, 0.0, 3.0][:n],
            "wind_speed_10m": 5.0,
            "wind_direction_10m": direction,
            "surface_pressure": 1000.0,
            "cloud_cover": 10.0,
        }
    )


def test_weather_is_relabelled_to_the_pm_hour_it_describes():
    out = weather_features(weather_table(), CFG)
    # weather at 01:00 describes the PM2.5 hour labelled 00:30
    assert out["time_utc"].iloc[1] == pd.Timestamp("2023-01-01 00:30")


def test_wind_direction_0_and_360_are_the_same():
    a = weather_features(weather_table(direction=0.0), CFG)
    b = weather_features(weather_table(direction=360.0), CFG)
    assert np.allclose(a["wind_dir_sin"], b["wind_dir_sin"])
    assert np.allclose(a["wind_dir_cos"], b["wind_dir_cos"])


def test_rain_is_summed_over_the_window():
    out = weather_features(weather_table(), CFG)
    assert np.isnan(out["rain_3h"].iloc[1])  # fewer than 3 hours available
    assert out["rain_3h"].tolist()[2:] == [3.0, 2.0, 5.0]


# ---------- calendar ----------


@pytest.mark.parametrize(
    ("utc", "hour", "festival"),
    [
        ("2023-01-01 10:30", 16, 0),  # 16:00 IST
        ("2023-11-14 18:00", 23, 1),  # 23:30 IST on Nov 14 = Diwali + 2
        ("2023-11-14 19:00", 0, 0),  # 00:30 IST on Nov 15 = Diwali + 3
        ("2023-11-09 18:30", 0, 1),  # 00:00 IST on Nov 10 = Diwali - 2
    ],
)
def test_calendar_uses_indian_time(utc, hour, festival):
    out = calendar_features(pd.Series(pd.to_datetime([utc])), CFG)
    assert out["hour"].iloc[0] == hour
    assert out["is_festival"].iloc[0] == festival


def test_weekend_flag():
    times = pd.Series(pd.to_datetime(["2023-01-07 06:30", "2023-01-09 06:30"]))
    out = calendar_features(times, CFG)  # Saturday, Monday (IST)
    assert out["is_weekend"].tolist() == [1, 0]


def test_storage_format_keeps_the_instant_and_labels_it_utc():
    df = pm_table([1.0]).astype({"city": "category"})
    out = for_storage(df)
    assert str(out["time_utc"].dtype) == "datetime64[us, UTC]"
    assert out["time_utc"].iloc[0] == pd.Timestamp("2023-01-01 00:30", tz="UTC")
    assert out["city"].dtype == object
