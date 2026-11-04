"""Forecast vs actual and drift (PSI) on made-up data (Stage 10)."""

import json
from datetime import UTC, datetime

import numpy as np
import pandas as pd

from airsense.monitoring.drift import drift_report, psi
from airsense.monitoring.evaluate import evaluate, load_forecasts, match

CFG = {
    "eval_window_days": 3,
    "error_ratio_limit": 1.5,
    "min_pairs": 4,
    "drift_days": 7,
    "drift_months": [10, 11],
    "min_live_rows": 50,
    "psi_moderate": 0.2,
    "psi_drift": 0.3,
}
NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


def saved(made, base_time, base, horizon, fc, city="Delhi"):
    t = pd.Timestamp(base_time, tz="UTC")
    return {
        "made_at_utc": pd.Timestamp(made, tz="UTC"),
        "model_version": "1",
        "city": city,
        "base_time_utc": t,
        "base_pm25": base,
        "horizon": horizon,
        "target_time_utc": t + pd.to_timedelta(horizon, unit="h"),
        "pm25_forecast": fc,
    }


def test_match_counts_each_forecast_once_and_scores_both(tmp_path):
    for hhmm, rows in {
        "0150": [saved("2026-11-05 01:50", "2026-11-05 00:30", 200, 1, 210)],
        "0250": [  # the 02:50 run saved the same forecast again (newer copy wins)
            saved("2026-11-05 02:50", "2026-11-05 00:30", 200, 1, 208),
            saved("2026-11-05 02:50", "2026-11-05 00:30", 200, 2, 212),
            saved(
                "2026-11-05 02:50", "2026-11-05 00:30", 200, 30, 250
            ),  # not measured yet
        ],
    }.items():
        (tmp_path / "2026-11-05").mkdir(exist_ok=True)
        pd.DataFrame(rows).to_parquet(tmp_path / "2026-11-05" / f"{hhmm}.parquet")
    f = load_forecasts(tmp_path)
    assert len(f) == 3
    actual = pd.DataFrame(
        {
            "city": "Delhi",
            "actual": [209.0, 215.0],
            "target_time_utc": pd.to_datetime(
                ["2026-11-05 01:30", "2026-11-05 02:30"], utc=True
            ),
        }
    )
    m = match(f, actual)
    assert len(m) == 2
    assert m["err_model"].tolist() == [1.0, 3.0]  # |208-209|, |212-215|
    assert m["err_no_change"].tolist() == [9.0, 15.0]  # |200-209|, |200-215|
    r = evaluate(m, {**CFG, "min_pairs": 2}, test_mae=6.8, now=NOW)
    assert r["overall"] == {
        "pairs": 2,
        "mae_model": 2.0,
        "mae_no_change": 12.0,
        "skill": 0.833,
    }
    assert r["error_high"] is False and r["limit_mae"] == 10.2
    assert r["by_horizon"]["1-6 h"]["pairs"] == 2 and r["daily"][0]["pairs"] == 2


def test_error_high_needs_big_error_worse_than_no_change_and_enough_pairs():
    def pairs(err_model, err_base, n):
        return pd.DataFrame(
            {
                "target_time_utc": pd.date_range(
                    "2026-11-05 00:30", periods=n, freq="h", tz="UTC"
                ),
                "horizon": 1,
                "city": "Delhi",
                "err_model": err_model,
                "err_no_change": err_base,
            }
        )

    assert evaluate(pairs(15.0, 12.0, 6), CFG, 6.8, NOW)["error_high"] is True
    assert (
        evaluate(pairs(15.0, 20.0, 6), CFG, 6.8, NOW)["error_high"] is False
    )  # beats no change
    assert (
        evaluate(pairs(9.0, 5.0, 6), CFG, 6.8, NOW)["error_high"] is False
    )  # under 1.5 x 6.8
    assert (
        evaluate(pairs(15.0, 12.0, 3), CFG, 6.8, NOW)["error_high"] is False
    )  # too few pairs
    empty = evaluate(pd.DataFrame(), CFG, 6.8, NOW)
    assert empty["overall"]["pairs"] == 0 and empty["error_high"] is False


def test_psi_is_small_for_the_same_distribution_and_large_for_a_shift():
    rng = np.random.default_rng(0)
    ref = rng.normal(100, 20, 5000)
    assert psi(ref, rng.normal(100, 20, 2000)) < 0.05
    assert 0.1 < psi(ref, rng.normal(110, 20, 2000)) < 0.4
    assert psi(ref, rng.normal(160, 20, 2000)) > 1.0
    rain_ref = np.where(rng.random(5000) < 0.9, 0.0, rng.exponential(3, 5000))
    assert np.isfinite(psi(rain_ref, np.zeros(500)))  # mostly-zero inputs still work


def test_drift_report_levels_and_not_enough_data():
    rng = np.random.default_rng(1)
    cols = [
        "pm25_24h",
        "pm25_change_24h",
        "temperature_2m",
        "relative_humidity_2m",
        "wind_speed_10m",
        "rain_24h",
    ]
    ref = pd.DataFrame({c: rng.normal(100, 20, 3000) for c in cols})
    live = pd.DataFrame({c: rng.normal(100, 20, 300) for c in cols})
    live["pm25_24h"] = rng.normal(180, 20, 300)  # e.g. a smoggy November
    live.loc[:280, "rain_24h"] = np.nan  # only 19 values
    r = drift_report(ref, live, CFG, NOW)
    assert r["inputs"]["pm25_24h"]["level"] == "drift" and r["drift"] is True
    assert r["inputs"]["temperature_2m"]["level"] == "stable"
    assert r["inputs"]["rain_24h"] == {
        "psi": None,
        "level": "not enough data",
        "live_rows": 19,
    }


def test_limit_uses_the_champions_test_error(tmp_path):
    from airsense.monitoring.evaluate import reference_test_mae

    metrics = tmp_path / "train_metrics.json"
    metrics.write_text(json.dumps({"test": {"model": {"mae": 9.9}}}))
    # the champion (served model) wins over the latest, maybe unpromoted, run
    assert reference_test_mae(lambda: (6.8, "1"), metrics) == (
        6.8,
        "champion version 1",
    )
    mae, source = reference_test_mae(lambda: None, metrics)  # registry unreadable
    assert mae == 9.9 and source.startswith("latest training run")
    assert reference_test_mae(lambda: None, tmp_path / "missing.json")[0] is None
