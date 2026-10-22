"""Unit tests for the weather validation helpers (Stage 3.3)."""

import pandas as pd

from airsense.validation.validate_weather import city_summary


def hourly(city, start, end):
    times = pd.date_range(start, end, freq="h")
    return pd.DataFrame({"city": city, "time_utc": times})


def test_complete_city_is_marked_complete():
    df = hourly("Delhi", "2020-01-01 00:00", "2020-01-02 23:00")
    s = city_summary(df, ["Delhi"], "2020-01-01", "2020-01-02")
    assert s["complete"].tolist() == [True]
    assert s["expected_hours"].iloc[0] == 48


def test_missing_hour_is_detected():
    df = hourly("Delhi", "2020-01-01 00:00", "2020-01-02 23:00").drop(index=10)
    s = city_summary(df, ["Delhi"], "2020-01-01", "2020-01-02")
    assert s["complete"].tolist() == [False]


def test_period_not_covered_to_the_end_is_detected():
    df = hourly("Delhi", "2020-01-01 00:00", "2020-01-02 22:00")  # last hour missing
    s = city_summary(df, ["Delhi"], "2020-01-01", "2020-01-02")
    assert not s["complete"].iloc[0]


def test_city_without_rows_is_reported():
    df = hourly("Delhi", "2020-01-01 00:00", "2020-01-01 23:00")
    s = city_summary(df, ["Delhi", "Pune"], "2020-01-01", "2020-01-01")
    assert s.set_index("city")["complete"].to_dict() == {"Delhi": True, "Pune": False}
