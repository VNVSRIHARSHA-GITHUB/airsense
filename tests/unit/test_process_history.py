"""Unit tests for the Spark cleaning functions (Stage 2.3)."""

from datetime import UTC, datetime, timedelta

import pytest
from pyspark.sql import functions as F

from airsense.processing.process_history import add_rolling_average, remove_stuck_runs

pytestmark = pytest.mark.spark
START = datetime(2022, 1, 1, tzinfo=UTC)


def hourly_frame(spark, values, station="S1", start=START, step_h=1):
    """Station rows with one value per hour (None = missing reading).

    Times are passed as text and parsed by Spark (session time zone UTC), so the
    test does not depend on the laptop's local time zone.
    """
    rows = [
        (
            station,
            (start + timedelta(hours=i * step_h)).strftime("%Y-%m-%d %H:%M:%S"),
            None if v is None else float(v),
        )
        for i, v in enumerate(values)
    ]
    df = spark.createDataFrame(rows, "station string, t string, pm25 double")
    return df.withColumn("time_utc", F.to_timestamp("t")).drop("t")


def collect(df, *cols):
    return [tuple(r) for r in df.orderBy("time_utc").select(*cols).collect()]


# ---------- remove_stuck_runs ----------


def test_long_identical_run_is_removed(spark):
    values = [1, 2] + [50] * 30 + [3, 4]
    out = collect(
        remove_stuck_runs(hourly_frame(spark, values), min_run=24), "pm25", "is_stuck"
    )
    assert sum(s for _, s in out) == 30
    assert all(v is None for v, s in out if s)  # stuck values blanked
    assert [v for v, _ in out[:2]] == [1.0, 2.0]  # others untouched


def test_short_identical_run_is_kept(spark):
    values = [1] + [50] * 10 + [2]
    out = collect(
        remove_stuck_runs(hourly_frame(spark, values), min_run=24), "is_stuck"
    )
    assert not any(s for (s,) in out)


def test_missing_value_breaks_a_run(spark):
    values = [50] * 20 + [None] + [50] * 20  # 41 rows, never 24 in a row
    out = collect(
        remove_stuck_runs(hourly_frame(spark, values), min_run=24), "is_stuck"
    )
    assert not any(s for (s,) in out)


def test_time_gap_breaks_a_run(spark):
    first = hourly_frame(spark, [50] * 20)
    second = hourly_frame(spark, [50] * 20, start=START + timedelta(hours=30))
    out = collect(remove_stuck_runs(first.unionByName(second), min_run=24), "is_stuck")
    assert not any(s for (s,) in out)


def test_runs_are_counted_per_station(spark):
    a = hourly_frame(spark, [50] * 15, station="A")
    b = hourly_frame(spark, [50] * 15, station="B")  # same times and value
    out = collect(remove_stuck_runs(a.unionByName(b), min_run=24), "is_stuck")
    assert not any(s for (s,) in out)  # 15 each, not 30 together


# ---------- add_rolling_average ----------


def test_rolling_mean_over_last_24_hours(spark):
    out = collect(
        add_rolling_average(hourly_frame(spark, range(1, 31)), 24, 16),
        "pm25_24h",
        "n_valid_24h",
    )
    assert out[23] == (12.5, 24)  # hours 1..24
    assert out[29] == (18.5, 24)  # hours 7..30: the window slides


def test_rolling_mean_needs_minimum_valid_hours(spark):
    out = collect(
        add_rolling_average(hourly_frame(spark, range(1, 31)), 24, 16),
        "pm25_24h",
        "n_valid_24h",
    )
    assert out[14] == (None, 15)  # 15 valid hours < 16 -> no average
    assert out[15] == (8.5, 16)  # 16 valid hours -> mean(1..16)


def test_missing_values_are_skipped_not_counted(spark):
    values = [10] * 20 + [None] * 4 + [10] * 4  # 28 hours, 4 missing
    out = collect(
        add_rolling_average(hourly_frame(spark, values), 24, 16),
        "pm25_24h",
        "n_valid_24h",
    )
    assert out[23] == (10.0, 20)  # window 0..23 holds 20 valid hours


def test_window_is_time_based_not_row_based(spark):
    # Readings every 2 hours: a 24-hour window holds 12 of them, not 24 rows.
    out = collect(
        add_rolling_average(hourly_frame(spark, range(1, 41), step_h=2), 24, 1),
        "n_valid_24h",
    )
    assert out[-1] == (12,)
