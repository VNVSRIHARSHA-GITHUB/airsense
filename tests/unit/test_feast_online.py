"""Unit tests for the online-store freshness helper (Stage 3.5)."""

import pandas as pd

from airsense.features.feast_online import compare_online_offline, newest_hours

NOW = pd.Timestamp("2026-10-22 10:00", tz="UTC")


def live(city, times, values):
    return pd.DataFrame(
        {
            "city": city,
            "time_utc": pd.to_datetime(times, utc=True),
            "pm25_24h": values,
        }
    )


def test_newest_hour_ignores_empty_rows_and_future_rows():
    df = live(
        "Delhi",
        ["2026-10-22 07:30", "2026-10-22 08:30", "2026-10-22 11:30"],
        [100.0, None, 120.0],  # 08:30 empty (grid row), 11:30 after NOW
    )
    assert newest_hours(df, NOW)["Delhi"] == pd.Timestamp("2026-10-22 07:30", tz="UTC")


def test_city_without_values_is_absent():
    df = pd.concat(
        [
            live("Delhi", ["2026-10-22 07:30"], [100.0]),
            live("Pune", ["2026-10-22 07:30"], [float("nan")]),
        ]
    )
    assert list(newest_hours(df, NOW).index) == ["Delhi"]


def test_identical_online_and_offline_values_pass():
    online = pd.DataFrame(
        {"city": ["Delhi"], "pm25_24h": [100.0], "lag": [float("nan")]}
    )
    assert compare_online_offline(online, online.copy(), ["pm25_24h", "lag"]) == []


def test_a_differing_value_is_reported():
    online = pd.DataFrame({"city": ["Delhi"], "pm25_24h": [100.0]})
    offline = pd.DataFrame({"city": ["Delhi"], "pm25_24h": [101.0]})
    problems = compare_online_offline(online, offline, ["pm25_24h"])
    assert problems == [
        {"city": "Delhi", "feature": "pm25_24h", "online": 100.0, "offline": 101.0}
    ]


def test_value_missing_on_one_side_only_is_reported():
    online = pd.DataFrame({"city": ["Delhi"], "pm25_24h": [float("nan")]})
    offline = pd.DataFrame({"city": ["Delhi"], "pm25_24h": [100.0]})
    assert len(compare_online_offline(online, offline, ["pm25_24h"])) == 1
