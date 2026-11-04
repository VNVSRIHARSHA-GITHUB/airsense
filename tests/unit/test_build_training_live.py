"""Unit tests for the live training rows (Stage 10)."""

import numpy as np
import pandas as pd

from airsense.features.build_training_live import (
    build,
    live_base,
    observed_weather,
    weather_columns,
)

CFG = {
    "rain_window_hours": 3,
    "weather_offset_minutes": 30,
    "local_timezone": "Asia/Kolkata",
    "festival_window_days": 2,
    "diwali_dates": ["2026-11-08"],
}
PARAMS = {
    "features": CFG,
    "forecast": {"horizon_hours": 3},
    "data": {"cities": {"Delhi": {"region": "North"}, "Pune": {"region": "West"}}},
}


def snapshot(folder, day, fetched, start, hours, temp):
    """One Open-Meteo snapshot: `hours` hourly rows from `start`, fetched at `fetched`."""
    t = pd.date_range(start, periods=hours, freq="h", tz="UTC")
    df = pd.DataFrame(
        {
            "city": "Delhi",
            "time_utc": t,
            "fetched_at_utc": pd.Timestamp(fetched, tz="UTC"),
            "temperature_2m": temp,
            "relative_humidity_2m": 50.0,
            "precipitation": 0.0,
            "wind_speed_10m": 5.0,
            "wind_direction_10m": 90.0,
            "surface_pressure": 1000.0,
            "cloud_cover": 10.0,
        }
    )
    (folder / day).mkdir(parents=True, exist_ok=True)
    df.to_csv(folder / day / f"{pd.Timestamp(fetched):%H%M}.csv", index=False)


def test_observed_weather_keeps_past_hours_and_the_newest_fetch(tmp_path):
    # fetched 05:00 covering 00:00-09:00 (05:00 on = forecast); then 07:00, warmer
    snapshot(tmp_path, "2026-11-01", "2026-11-01 05:00", "2026-11-01 00:00", 10, 20.0)
    snapshot(tmp_path, "2026-11-01", "2026-11-01 07:00", "2026-11-01 03:00", 10, 25.0)
    w = observed_weather(tmp_path, CFG)
    # labels move back 30 min (weather_offset): 11:00 describes the 10:30 PM2.5 hour
    assert w["time_utc"].max() == pd.Timestamp("2026-11-01 06:30", tz="UTC")
    assert w["time_utc"].is_unique
    by_time = w.set_index("time_utc")["temperature_2m"]
    assert (
        by_time[pd.Timestamp("2026-11-01 01:30", tz="UTC")] == 20.0
    )  # only first fetch
    assert (
        by_time[pd.Timestamp("2026-11-01 05:30", tz="UTC")] == 25.0
    )  # newest fetch wins
    # the first 2 hours have no 3-hour rain total in any snapshot: left out
    assert w["time_utc"].min() == pd.Timestamp("2026-11-01 01:30", tz="UTC")
    assert set(weather_columns(CFG)) <= set(w.columns)
    assert observed_weather(tmp_path / "none", CFG).empty


def test_live_rows_have_targets_and_complete_weather_only():
    t = pd.date_range("2026-11-01 00:30", periods=8, freq="h", tz="UTC")
    pm = pd.DataFrame(
        {"city": "Delhi", "time_utc": t, "pm25_24h": np.arange(100.0, 108.0)}
    )
    cols = weather_columns(CFG)
    weather = pd.DataFrame({"city": "Delhi", "time_utc": t, **{c: 1.0 for c in cols}})
    weather.loc[7, "temperature_2m"] = np.nan  # last hour: incomplete weather
    base = live_base(pm, weather, CFG)
    assert len(base) == 7
    parts = build(base, PARAMS)
    assert sorted(parts) == [1, 2, 3]
    h2 = parts[2]
    assert len(h2) == 5  # 7 hours, the last two have no target 2 h later
    assert (h2["target"].to_numpy() == h2["pm25_24h"].to_numpy() + 2).all()
    assert (h2["region"] == "North").all()
    assert h2["temperature_2m_now"].dtype == np.float32
    assert {"temperature_2m_target", "horizon", "target_time_utc"} <= set(h2.columns)


def test_an_hour_keeps_its_complete_weather_from_an_older_fetch(tmp_path):
    # rain window 3 h: the 07:00 snapshot starting 03:00 has no rain total for its
    # first 2 hours; there the older, complete 05:00 snapshot is used
    snapshot(tmp_path, "2026-11-01", "2026-11-01 05:00", "2026-11-01 00:00", 10, 20.0)
    snapshot(tmp_path, "2026-11-01", "2026-11-01 07:00", "2026-11-01 03:00", 10, 25.0)
    w = observed_weather(tmp_path, CFG).set_index("time_utc")
    assert w.loc[pd.Timestamp("2026-11-01 02:30", tz="UTC"), "temperature_2m"] == 20.0
    assert w.loc[pd.Timestamp("2026-11-01 04:30", tz="UTC"), "temperature_2m"] == 25.0
    assert w["rain_3h"].notna().all()
