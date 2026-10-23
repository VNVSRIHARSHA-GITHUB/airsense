"""Unit tests for the sanity gate and the promotion rule (Stage 5)."""

import copy

import pytest

from airsense.training.gate import run_checks
from airsense.training.register import decide

CFG = {
    "min_skill": 0.05,
    "horizon_tolerance": 1.05,
    "min_city_skill": -0.05,
    "min_aqi_accuracy": 0.70,
    "prediction_range": [0, 1000],
    "max_no_forecast_share": 0.01,
}

GOOD = {
    "val": {"model": {"mae": 4.5, "skill": 0.20, "aqi_accuracy": 0.85}},
    "val_by_horizon": {
        "model": {"1": {"mae": 0.8}, "24": {"mae": 10.0}},
        "persistence": {"1": {"mae": 0.9}, "24": {"mae": 12.0}},
    },
    "val_by_city": {"Delhi": {"skill": 0.15}, "Pune": {"skill": 0.02}},
    "prediction_check": {
        "val": {"non_finite": 0, "no_forecast_share": 0.001, "min": 3.0, "max": 640.0}
    },
}


def failed(metrics):
    return [r["check"] for r in run_checks(metrics, CFG) if not r["passed"]]


def test_good_model_passes_every_check():
    assert failed(GOOD) == []


def test_model_worse_than_persistence_at_short_horizons_fails():
    """The first Stage 4 model (h1: 3.0 vs 0.9) would have been stopped here."""
    m = copy.deepcopy(GOOD)
    m["val_by_horizon"]["model"]["1"]["mae"] = 3.0
    assert failed(m) == ["every_horizon_vs_persistence"]


def test_small_noise_within_tolerance_passes():
    m = copy.deepcopy(GOOD)
    m["val_by_horizon"]["model"]["1"]["mae"] = 0.94  # 4% worse: within 5%
    assert failed(m) == []


def test_city_clearly_worse_than_persistence_fails():
    m = copy.deepcopy(GOOD)
    m["val_by_city"]["Pune"]["skill"] = -0.10
    assert failed(m) == ["every_city_vs_persistence"]


def test_too_many_hours_without_a_forecast_fail():
    m = copy.deepcopy(GOOD)
    m["prediction_check"]["val"]["no_forecast_share"] = 0.05
    assert failed(m) == ["predictions_valid"]


def test_low_skill_aqi_and_bad_predictions_fail():
    m = copy.deepcopy(GOOD)
    m["val"]["model"].update(skill=0.01, aqi_accuracy=0.6)
    m["prediction_check"]["val"].update(non_finite=3)
    assert failed(m) == ["overall_skill", "aqi_category_accuracy", "predictions_valid"]


@pytest.mark.parametrize(
    ("challenger", "champion", "promote"),
    [
        (5.0, None, True),  # no champion yet
        (4.90, 5.0, True),  # 2% better
        (4.96, 5.0, False),  # 0.8% better: noise, keep champion
        (5.10, 5.0, False),  # worse
        (5.0, float("inf"), True),  # champion cannot be scored on the new data
    ],
)
def test_promotion_rule(challenger, champion, promote):
    assert decide(challenger, champion, 0.01)[0] is promote
