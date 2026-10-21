"""Unit tests for live snapshot processing (Stage 2.4)."""

import pandas as pd

from airsense.processing.process_live import (
    deduplicate,
    to_city_hours,
    to_station_hours,
    write_parquet,
)

CITY_COLUMNS = [
    "city",
    "time_utc",
    "pm25_24h",
    "n_stations_24h",
    "pm25_1h",
    "n_stations_1h",
    "n_capped",
]


def snapshot_rows(**overrides):
    """One raw record as the collector saves it (all values as text)."""
    row = {
        "country": "India",
        "state": "Delhi",
        "city": "Delhi",
        "station": "Alipur, Delhi - DPCC",
        "last_update": "20-10-2026 16:00:00",
        "latitude": "28.8",
        "longitude": "77.1",
        "pollutant_id": "PM2.5",
        "min_value": "55",
        "max_value": "323",
        "avg_value": "190",
        "fetched_at_utc": "2026-10-20T11:00:00+00:00",
    }
    row.update(overrides)
    return row


def test_time_is_converted_to_utc_with_offset():
    out = to_station_hours(pd.DataFrame([snapshot_rows()]), ["Delhi"], offset_h=-1)
    # 16:00 IST = 10:30 UTC; the -1 h label convention gives 09:30.
    assert out["time_utc"].iloc[0] == pd.Timestamp("2026-10-20 09:30:00")
    assert out["time_utc"].dt.tz is None  # naive UTC, like the history


def test_only_pm25_of_configured_cities_is_kept():
    raw = pd.DataFrame(
        [
            snapshot_rows(),
            snapshot_rows(pollutant_id="NO2"),
            snapshot_rows(city="Agra", station="X, Agra - UPPCB"),
        ]
    )
    out = to_station_hours(raw, ["Delhi"], offset_h=-1)
    assert len(out) == 1 and out["city"].iloc[0] == "Delhi"


def test_names_are_stripped_and_values_numeric():
    raw = pd.DataFrame(
        [
            snapshot_rows(city=" Delhi ", avg_value="190"),
            snapshot_rows(avg_value="NA", last_update="20-10-2026 17:00:00"),
        ]
    )
    out = to_station_hours(raw, ["Delhi"], offset_h=-1)
    assert len(out) == 1  # "NA" row dropped
    assert out["avg_value"].iloc[0] == 190.0


def test_deduplicate_keeps_latest_fetch_and_counts_conflicts():
    raw = pd.DataFrame(
        [
            snapshot_rows(avg_value="190", fetched_at_utc="2026-10-20T11:00:00+00:00"),
            snapshot_rows(avg_value="195", fetched_at_utc="2026-10-20T11:30:00+00:00"),
            snapshot_rows(last_update="20-10-2026 17:00:00", avg_value="200"),
            snapshot_rows(
                last_update="20-10-2026 17:00:00",
                avg_value="200",
                fetched_at_utc="2026-10-20T12:05:00+00:00",
            ),
        ]
    )
    rows, repeats, conflicts = deduplicate(to_station_hours(raw, ["Delhi"], -1))
    assert len(rows) == 2 and repeats == 2
    assert conflicts == 1  # 16:00 was revised 190 -> 195
    assert rows.sort_values("time_utc")["avg_value"].iloc[0] == 195.0


def test_city_hours_average_stations():
    stations = pd.DataFrame(
        {
            "city": ["Delhi", "Delhi", "Delhi"],
            "time_utc": pd.to_datetime(["2026-10-20 09:30"] * 2 + ["2026-10-20 10:30"]),
            "pm25_24h": [100.0, 200.0, 50.0],
        }
    )
    city = to_city_hours(stations).sort_values("time_utc")
    assert list(city.columns) == CITY_COLUMNS  # same as the history table
    assert city["pm25_24h"].tolist() == [150.0, 50.0]
    assert city["n_stations_24h"].tolist() == [2, 1]


def test_write_parquet_replaces_instead_of_appending(tmp_path):
    df = pd.DataFrame({"city": ["Delhi", "Pune"], "value": [1, 2]})
    out = tmp_path / "table"
    write_parquet(df, out)
    write_parquet(df, out)  # second run: same result
    assert len(pd.read_parquet(out)) == 2
