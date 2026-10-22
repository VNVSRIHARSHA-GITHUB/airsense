"""Unit tests for the horizon expansion of the training table (Stage 3.6)."""

import numpy as np
import pandas as pd
from airsense.features.build_training import compact, entity_rows, expand_horizon

CAL = {
    "local_timezone": "Asia/Kolkata",
    "festival_window_days": 2,
    "diwali_dates": ["2023-11-12"],
}
WEATHER = ["temperature_2m"]


def base_table(n=6, city="Delhi"):
    """A Feast-like result: one row per hour with a PM2.5 feature and weather."""
    return pd.DataFrame(
        {
            "city": city,
            "time_utc": pd.date_range(
                "2023-01-01 00:30", periods=n, freq="h", tz="UTC"
            ),
            "pm25_24h": np.arange(n, dtype=float) * 10,  # 0, 10, 20, ...
            "pm25_lag_1h": np.arange(n, dtype=float) * 10 - 10,
            "temperature_2m": np.arange(n, dtype=float) + 100,  # 100, 101, ...
        }
    )


def test_target_and_target_weather_come_from_t_plus_h():
    out = expand_horizon(base_table(), 2, WEATHER, {"Delhi": "North"}, CAL)
    first = out.iloc[0]
    assert first["time_utc"] == pd.Timestamp("2023-01-01 00:30", tz="UTC")
    assert first["target"] == 20.0  # pm25_24h at 02:30
    assert first["temperature_2m_target"] == 102.0  # weather at 02:30
    assert first["temperature_2m_now"] == 100.0  # weather at 00:30
    assert first["pm25_24h"] == 0.0  # the feature stays at t


def test_rows_without_a_target_hour_are_dropped():
    out = expand_horizon(base_table(n=6), 2, WEATHER, {"Delhi": "North"}, CAL)
    assert len(out) == 4  # the last 2 hours have no t + 2 in the data


def test_missing_target_is_dropped_not_filled():
    base = base_table()
    base.loc[3, "pm25_24h"] = np.nan  # hour 03:30 has no value
    out = expand_horizon(base, 1, WEATHER, {"Delhi": "North"}, CAL)
    assert pd.Timestamp("2023-01-01 03:30", tz="UTC") not in set(out["target_time_utc"])
    assert out["target"].notna().all()


def test_calendar_and_region_describe_the_target_hour():
    out = expand_horizon(base_table(), 1, WEATHER, {"Delhi": "North"}, CAL)
    # target 01:30 UTC = 07:00 IST
    assert out.loc[0, "hour"] == 7
    assert out.loc[0, "region"] == "North"
    assert (out["horizon"] == 1).all()


def test_cities_are_not_mixed():
    base = pd.concat(
        [base_table(city="Delhi"), base_table(city="Pune").assign(pm25_24h=7.0)]
    )
    out = expand_horizon(base, 1, WEATHER, {"Delhi": "North", "Pune": "West"}, CAL)
    assert (out.loc[out["city"] == "Pune", "target"] == 7.0).all()


def test_entity_rows_start_at_train_start():
    pm = base_table().assign(city=lambda d: d["city"].astype("category"))
    rows = entity_rows(pm, "2023-01-01 02:00")
    assert rows["event_timestamp"].min() == pd.Timestamp("2023-01-01 02:30", tz="UTC")
    assert rows["city"].dtype == object


def test_compact_shrinks_types():
    out = compact(expand_horizon(base_table(), 1, WEATHER, {"Delhi": "North"}, CAL))
    assert out["pm25_24h"].dtype == np.float32
    assert out["hour"].dtype == np.int16
    assert str(out["time_utc"].dtype) == "datetime64[us, UTC]"
