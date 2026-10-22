"""Unit tests for the training helpers (Stage 4)."""

import numpy as np
import pandas as pd
import pytest

from airsense.training.train import (
    aqi_category,
    category_codes,
    encode_categories,
    score,
    split_masks,
    to_level,
    training_label,
    yesterday_baseline,
)

SPLIT = {
    "train_end": "2022-01-01",
    "val_start": "2022-01-02",
    "val_end": "2022-10-01",
    "test_start": "2022-10-02",
}


def rows(start, end, horizons=(1, 24), city="Delhi"):
    t = pd.date_range(start, end, freq="h", tz="UTC")
    parts = [
        pd.DataFrame({"city": city, "time_utc": t, "horizon": h}).assign(
            target_time_utc=lambda d, h=h: d["time_utc"] + pd.to_timedelta(h, unit="h")
        )
        for h in horizons
    ]
    return pd.concat(parts, ignore_index=True)


def test_split_sets_do_not_overlap_in_time():
    df = rows("2021-12-25 00:30", "2022-10-10 00:30")
    p = {k: df[m] for k, m in split_masks(df, SPLIT).items()}
    assert p["train"]["target_time_utc"].max() < pd.Timestamp("2022-01-01", tz="UTC")
    assert p["val"]["time_utc"].min() >= pd.Timestamp("2022-01-02", tz="UTC")
    assert p["val"]["target_time_utc"].max() < pd.Timestamp("2022-10-01", tz="UTC")
    assert p["test"]["time_utc"].min() >= pd.Timestamp("2022-10-02", tz="UTC")
    # the gap: a train target and a validation forecast are at least 24 h apart
    gap = p["val"]["time_utc"].min() - p["train"]["target_time_utc"].max()
    assert gap >= pd.to_timedelta(24, unit="h")


def test_aqi_categories_follow_cpcb_bands():
    values = [0, 30, 30.5, 60, 61, 90, 120, 121, 250, 251, np.nan]
    assert aqi_category(values).tolist() == [0, 0, 1, 1, 2, 2, 3, 4, 4, 5, -1]


def test_score_mae_skill_and_aqi_accuracy():
    y = [100.0, 200.0]
    s = score(y, pred=[110.0, 190.0], persistence=[80.0, 240.0])
    assert s["mae"] == 10.0
    assert s["skill"] == pytest.approx(1 - 10 / 30)
    assert s["aqi_accuracy"] == 1.0  # 110 and 100 Poor; 190 and 200 Very Poor


def test_score_ignores_missing_values():
    s = score([100.0, np.nan], pred=[90.0, 50.0], persistence=[np.nan, np.nan])
    assert s["n"] == 1 and s["mae"] == 10.0 and "skill" not in s


def test_yesterday_baseline_looks_up_t_plus_h_minus_24():
    df = rows("2022-01-01 00:30", "2022-01-03 00:30", horizons=(1, 5))
    df["city"] = 0  # city is already a code when the baseline is computed
    df["pm25_24h"] = df["time_utc"].dt.hour.astype(float)  # value = hour of day
    out = yesterday_baseline(df)
    row = df[(df["horizon"] == 5) & (df["time_utc"] == "2022-01-02 10:30+00:00")]
    # target 15:30 on Jan 2 -> yesterday = 15:30 on Jan 1 -> value 15
    assert out.loc[row.index[0]] == 15.0


def test_category_codes_are_fixed_and_unknown_values_rejected():
    params = {
        "data": {
            "cities": {
                "Delhi": {"region": "North"},
                "Pune": {"region": "West"},
            }
        }
    }
    codes = category_codes(params)
    assert codes == {
        "city": {"Delhi": 0, "Pune": 1},
        "region": {"North": 0, "West": 1},
    }
    df = pd.DataFrame({"city": ["Pune", "Delhi"], "region": ["West", "North"]})
    df = df.astype("category")  # as read from the training files
    encode_categories(df, codes)  # in place
    assert df["city"].tolist() == [1, 0] and df["region"].tolist() == [1, 0]
    assert df["city"].dtype == np.int16
    with pytest.raises(ValueError):
        encode_categories(pd.DataFrame({"city": ["Agra"], "region": ["North"]}), codes)


def test_feature_matrix_selects_rows_and_keeps_column_order():
    from airsense.training.train import feature_matrix

    df = pd.DataFrame({"a": [1.0, 2.0, 3.0], "b": np.array([10, 20, 30], np.int16)})
    x = feature_matrix(df, ["b", "a"], np.array([True, False, True]))
    assert x.dtype == np.float32
    assert x.tolist() == [[10.0, 1.0], [30.0, 3.0]]


def test_change_mode_learns_the_difference_and_adds_it_back():
    target, current = [110.0, 90.0], [100.0, 100.0]
    label = training_label(target, current, "change")
    assert label.tolist() == [10.0, -10.0]
    assert to_level(label, current, "change").tolist() == target
    assert training_label(target, current, "level").tolist() == target
    with pytest.raises(ValueError):
        training_label(target, current, "ratio")
