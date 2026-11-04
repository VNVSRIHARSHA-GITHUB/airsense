"""Unit tests for the raw CPCB station checks (Stage 2.2): the two expectation
suites on small station files, run with an in-memory Great Expectations context."""

import logging

import great_expectations as gx
import numpy as np
import pandas as pd
import pytest

from airsense.validation.validate_raw import (
    PM,
    TIME_COL,
    build_suites,
    failed_checks,
)

CFG = {"max_missing_frac": 0.5, "cap_value": 1000, "min_below_cap_frac": 0.999}


@pytest.fixture(scope="module")
def validate():
    logging.getLogger("great_expectations").setLevel(logging.WARNING)
    context = gx.get_context(mode="ephemeral")
    batch_def = (
        context.data_sources.add_pandas("test_raw")
        .add_dataframe_asset(name="station_file")
        .add_batch_definition_whole_dataframe("whole_file")
    )
    critical, warning = build_suites(CFG)

    def run(df):
        batch = batch_def.get_batch(batch_parameters={"dataframe": df})
        crit = batch.validate(critical)
        warn = failed_checks(batch.validate(warning)) if crit.success else []
        return crit.success, failed_checks(crit), warn

    return run


def station(n=48, pm=80.0):
    times = pd.date_range("2022-01-01", periods=n, freq="h")
    return pd.DataFrame(
        {TIME_COL: times.strftime("%Y-%m-%d %H:%M:%S"), PM: np.full(n, pm)}
    )


def test_a_clean_station_passes_without_warnings(validate):
    ok, failed, warn = validate(station())
    assert ok and failed == [] and warn == []


def test_negative_values_and_repeated_hours_quarantine_a_station(validate):
    df = station()
    df.loc[3, PM] = -5.0
    ok, failed, _ = validate(df)
    assert not ok
    assert [f["column"] for f in failed] == [PM]

    df = station()
    df.loc[5, TIME_COL] = df.loc[4, TIME_COL]  # the same hour twice
    ok, failed, _ = validate(df)
    assert not ok and failed[0]["expectation"] == "expect_column_values_to_be_unique"


def test_missing_pm_column_is_critical(validate):
    ok, failed, warn = validate(station().drop(columns=[PM]))
    assert not ok and warn == []  # warning checks need the critical columns
    assert any(f["expectation"] == "expect_column_to_exist" for f in failed)


def test_many_gaps_and_values_at_the_cap_only_warn(validate):
    df = station(n=100)
    df.loc[:59, PM] = np.nan  # 60 % empty (limit 50 %)
    df.loc[90:, PM] = 1000.0  # 10 of the 40 values at the cap
    ok, failed, warn = validate(df)
    assert ok and failed == []
    kinds = sorted(w["expectation"] for w in warn)
    assert kinds == [
        "expect_column_values_to_be_between",
        "expect_column_values_to_not_be_null",
    ]
    assert all(w["unexpected_percent"] is not None for w in warn)
