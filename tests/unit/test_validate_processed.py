"""Unit tests for the processed-data validation helpers (Stage 2.5)."""

import pandas as pd

from airsense.validation.validate_processed import (
    add_derived,
    history_summary,
    live_summary,
    schema_problems,
)


def city_table(**overrides):
    data = {
        "city": ["Delhi", "Delhi"],
        "time_utc": pd.to_datetime(["2020-01-01 00:00", "2020-01-01 01:00"]),
        "pm25_24h": [100.0, None],
        "n_stations_24h": [3, 0],
        "pm25_1h": [90.0, None],
        "n_stations_1h": [3, 0],
        "n_capped": [0, 0],
    }
    data.update(overrides)
    return pd.DataFrame(data)


def test_value_without_station_is_flagged():
    df = add_derived(city_table(pm25_24h=[100.0, 80.0], n_stations_24h=[3, 0]))
    assert df["value_without_station"].tolist() == [False, True]


def test_missing_value_with_zero_stations_is_fine():
    assert not add_derived(city_table())["value_without_station"].any()


def test_history_summary_counts_coverage_since_date():
    df = city_table(
        time_utc=pd.to_datetime(["2019-12-31 23:00", "2020-01-01 01:00"]),
        pm25_24h=[100.0, None],
    )
    s = history_summary(df, "2020-01-01")
    assert s.loc[s.city == "Delhi", "coverage_frac"].iloc[0] == 0.0  # 2019 row excluded


def test_live_summary_age_and_min_stations():
    df = city_table(pm25_24h=[100.0, 120.0], n_stations_24h=[3, 1])
    s = live_summary(df, now_utc=pd.Timestamp("2020-01-01 04:00"))
    assert s["age_hours"].iloc[0] == 3.0  # newest hour 01:00
    assert s["min_stations"].iloc[0] == 1


def test_schema_problems_detects_type_and_order_differences():
    history = city_table()
    assert schema_problems(history, history.copy()) == []
    live = history.astype({"n_stations_24h": "float64"})
    assert any("n_stations_24h" in p for p in schema_problems(history, live))
    assert any(
        "order" in p for p in schema_problems(history, history[history.columns[::-1]])
    )
