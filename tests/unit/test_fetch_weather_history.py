"""Unit tests for the historical weather download helpers (Stage 3.2)."""

import pandas as pd
import pytest
from airsense.ingestion.fetch_weather_history import (
    expected_rows,
    to_naive_utc,
    year_ranges,
)


def test_range_is_split_into_calendar_years_and_clipped():
    assert year_ranges("2017-03-01", "2019-06-30") == [
        ("2017-03-01", "2017-12-31"),
        ("2018-01-01", "2018-12-31"),
        ("2019-01-01", "2019-06-30"),
    ]


def test_single_day_range():
    assert year_ranges("2020-02-29", "2020-02-29") == [("2020-02-29", "2020-02-29")]


def test_reversed_range_is_rejected():
    with pytest.raises(ValueError):
        year_ranges("2020-01-02", "2020-01-01")


def test_expected_rows_counts_leap_years():
    assert expected_rows("2020-01-01", "2020-12-31", n_cities=14) == 366 * 24 * 14
    assert expected_rows("2021-01-01", "2021-12-31", n_cities=1) == 365 * 24


def test_utc_label_is_removed_but_time_unchanged():
    df = pd.DataFrame({"time_utc": pd.to_datetime(["2020-01-01 05:00"], utc=True)})
    out = to_naive_utc(df)
    assert out["time_utc"].iloc[0] == pd.Timestamp("2020-01-01 05:00")
    assert out["time_utc"].dt.tz is None
