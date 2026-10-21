"""Validate the processed city-level data with Great Expectations (Stage 2.5).

Checks the OUTPUTS of the processing steps (2.3 history, 2.4 live), to confirm the
transformations themselves did not break anything. Same two levels as the raw
validation (design choice B):

CRITICAL (any failure -> exit code 1, the pipeline stops):
  - both tables have exactly the expected columns, with identical data types;
  - only the configured cities appear;
  - no missing timestamps and no duplicate (city, hour) rows;
  - 24-hour PM2.5 within [0, cap];
  - every 24-hour value is backed by at least one station.

WARNING (reported, the pipeline continues):
  - history: coverage of the 24-hour average since a date, per city;
  - live: every city present; newest hour recent enough; enough stations per hour.

City-level checks run on a small per-city summary table, so every check, row-level
or city-level, is a Great Expectations expectation with the same kind of report.

Output: data/interim/processed_validation.json
"""

import json
import logging
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import great_expectations as gx
import pandas as pd
import yaml
from great_expectations.data_context.types.base import ProgressBarsConfig

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
HISTORY = PROJECT_ROOT / "data" / "processed" / "city_hourly"
LIVE = PROJECT_ROOT / "data" / "processed" / "live_city_hourly"
OUT_FILE = PROJECT_ROOT / "data" / "interim" / "processed_validation.json"

COLUMNS = [
    "city",
    "time_utc",
    "pm25_24h",
    "n_stations_24h",
    "pm25_1h",
    "n_stations_1h",
    "n_capped",
]


def row_suite(name: str, cities: list[str], cap: float) -> gx.ExpectationSuite:
    """Critical row-level checks, identical for history and live."""
    e = gx.expectations
    return gx.ExpectationSuite(
        name=name,
        expectations=[
            # The checked frame also holds the derived column added by add_derived.
            e.ExpectTableColumnsToMatchSet(
                column_set=[*COLUMNS, "value_without_station"], exact_match=True
            ),
            e.ExpectColumnValuesToBeInSet(column="city", value_set=cities),
            e.ExpectColumnValuesToNotBeNull(column="time_utc"),
            e.ExpectCompoundColumnsToBeUnique(column_list=["city", "time_utc"]),
            e.ExpectColumnValuesToBeBetween(
                column="pm25_24h", min_value=0, max_value=cap
            ),
            e.ExpectColumnValuesToBeBetween(column="n_stations_24h", min_value=0),
            # Derived column (see add_derived): a value with no station behind it.
            e.ExpectColumnValuesToBeInSet(
                column="value_without_station", value_set=[False]
            ),
        ],
    )


def history_summary_suite(min_frac: float) -> gx.ExpectationSuite:
    """Warning checks on the per-city history summary."""
    return gx.ExpectationSuite(
        name="history_city_warning",
        expectations=[
            gx.expectations.ExpectColumnValuesToBeBetween(
                column="coverage_frac", min_value=min_frac
            ),
        ],
    )


def live_summary_suite(
    cities: list[str], max_age_h: float, min_stations: int
) -> gx.ExpectationSuite:
    """Warning checks on the per-city live summary."""
    e = gx.expectations
    return gx.ExpectationSuite(
        name="live_city_warning",
        expectations=[
            e.ExpectColumnDistinctValuesToContainSet(column="city", value_set=cities),
            e.ExpectColumnValuesToBeBetween(column="age_hours", max_value=max_age_h),
            e.ExpectColumnValuesToBeBetween(
                column="min_stations", min_value=min_stations
            ),
        ],
    )


def add_derived(df: pd.DataFrame) -> pd.DataFrame:
    """Columns needed by the checks (GX checks columns, not conditions between them)."""
    out = df.copy()
    out["city"] = out["city"].astype(str)  # stored as a category in Parquet
    out["value_without_station"] = out["pm25_24h"].notna() & (out["n_stations_24h"] < 1)
    return out


def history_summary(df: pd.DataFrame, since: str) -> pd.DataFrame:
    """Per city: fraction of hours since `since` that have a 24-hour average."""
    recent = df[df["time_utc"] >= pd.Timestamp(since)]
    return (
        recent.groupby("city")["pm25_24h"]
        .agg(coverage_frac=lambda s: s.notna().mean())
        .reset_index()
    )


def live_summary(df: pd.DataFrame, now_utc: pd.Timestamp) -> pd.DataFrame:
    """Per city: age of the newest live hour, and the fewest stations in any hour."""
    s = (
        df.dropna(subset=["pm25_24h"])
        .groupby("city")
        .agg(newest=("time_utc", "max"), min_stations=("n_stations_24h", "min"))
        .reset_index()
    )
    s["age_hours"] = (now_utc - s["newest"]).dt.total_seconds() / 3600
    return s


def failed_checks(result) -> list[dict]:
    """Summarise only the failed expectations of a validation result."""
    failed = []
    for r in result.results:
        if not r.success:
            res = r.result or {}
            pct = res.get("unexpected_percent")
            failed.append(
                {
                    "expectation": r.expectation_config.type,
                    "column": r.expectation_config.kwargs.get("column"),
                    "unexpected_percent": None if pct is None else round(float(pct), 3),
                    "examples": [
                        str(v) for v in (res.get("partial_unexpected_list") or [])[:5]
                    ],
                    "observed": None
                    if "observed_value" not in res
                    else str(res["observed_value"]),
                }
            )
    return failed


def schema_problems(history: pd.DataFrame, live: pd.DataFrame) -> list[str]:
    """Differences in column order or data type between the two tables."""
    problems = []
    if list(history.columns) != list(live.columns):
        problems.append(
            f"column order differs: {list(history.columns)} vs {list(live.columns)}"
        )
    for col in set(history.columns) & set(live.columns):
        if col != "city" and history[col].dtype != live[col].dtype:
            problems.append(
                f"{col}: {history[col].dtype} (history) vs {live[col].dtype} (live)"
            )
    return problems


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logging.getLogger("great_expectations").setLevel(logging.WARNING)
    with open(PROJECT_ROOT / "params.yaml") as f:
        params = yaml.safe_load(f)
    cities = sorted(params["data"]["cities"])
    cap = params["validation"]["raw"]["cap_value"]
    cfg = params["validation"]["processed"]

    start = time.perf_counter()
    history = pd.read_parquet(HISTORY)
    live = pd.read_parquet(LIVE)
    now_utc = pd.Timestamp.now(tz="UTC").tz_localize(None)  # naive UTC, like the data

    context = gx.get_context(mode="ephemeral")
    context.variables.progress_bars = ProgressBarsConfig(globally=False)
    batch_def = (
        context.data_sources.add_pandas("processed")
        .add_dataframe_asset(name="table")
        .add_batch_definition_whole_dataframe("whole_table")
    )

    def validate(df: pd.DataFrame, suite: gx.ExpectationSuite) -> list[dict]:
        batch = batch_def.get_batch(batch_parameters={"dataframe": df})
        return failed_checks(batch.validate(suite))

    critical = {
        "schema": schema_problems(history, live),
        "history_rows": validate(
            add_derived(history), row_suite("history_rows", cities, cap)
        ),
        "live_rows": validate(add_derived(live), row_suite("live_rows", cities, cap)),
    }
    h_sum = history_summary(add_derived(history), cfg["coverage_since"])
    l_sum = live_summary(add_derived(live), now_utc)
    warnings = {
        "history_coverage": validate(
            h_sum, history_summary_suite(cfg["min_coverage_frac"])
        ),
        "live_status": validate(
            l_sum,
            live_summary_suite(
                cities, cfg["max_live_age_hours"], cfg["min_live_stations"]
            ),
        ),
    }
    # Which cities triggered each warning (GX reports the values, not the cities).
    details = {
        "low_history_coverage": h_sum.loc[
            h_sum["coverage_frac"] < cfg["min_coverage_frac"], ["city", "coverage_frac"]
        ]
        .round(3)
        .to_dict("records"),
        "missing_live_cities": sorted(set(cities) - set(l_sum["city"])),
        "stale_live_cities": l_sum.loc[
            l_sum["age_hours"] > cfg["max_live_age_hours"], ["city", "age_hours"]
        ]
        .round(1)
        .to_dict("records"),
        "few_live_stations": l_sum.loc[
            l_sum["min_stations"] < cfg["min_live_stations"], ["city", "min_stations"]
        ].to_dict("records"),
    }
    elapsed = time.perf_counter() - start
    ok = not any(critical.values())

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(
        json.dumps(
            {
                "validated_at_utc": datetime.now(UTC).isoformat(),
                "settings": {**cfg, "cap_value": cap},
                "rows": {"history": len(history), "live": len(live)},
                "seconds": round(elapsed, 1),
                "critical_passed": ok,
                "critical": critical,
                "warnings": warnings,
                "warning_details": details,
            },
            indent=2,
            default=str,
        )
    )

    for name, problems in critical.items():
        for p in problems:
            log.error("CRITICAL %s: %s", name, p)
    for name, items in details.items():
        if items:
            log.warning("WARNING %s: %s", name, items)
    log.info(
        "Validated %d history + %d live rows in %.1f s: critical %s, %d warning(s)",
        len(history),
        len(live),
        elapsed,
        "PASSED" if ok else "FAILED",
        sum(len(v) for v in warnings.values()),
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
