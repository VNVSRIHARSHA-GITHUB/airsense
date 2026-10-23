"""Validate the historical weather (Open-Meteo archive) with Great Expectations (3.3).

Same two levels as the other validation steps (design choice B):

CRITICAL (any failure -> exit code 1, the pipeline stops):
  - exactly the expected columns;
  - only the configured cities; no missing timestamps; one row per (city, hour);
  - every city has every hour from start_date to end_date (complete hourly grid);
  - values physically possible (humidity and cloud cover 0-100 %, wind direction
    0-360 deg, rain and wind speed >= 0, temperature and pressure in wide limits).

WARNING (reported, the pipeline continues; thresholds in params.yaml):
  - missing values per variable;
  - values outside the range plausible for Indian cities (e.g. temperature,
    pressure, extreme wind or rain), which flags suspicious data, not errors.

The physical limits are definitions (humidity can never exceed 100 %), so they
live in the code; the plausibility limits are judgement calls, so they live in
params.yaml (weather.validation).

Output: data/interim/weather_validation.json
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

from airsense.ingestion.openmeteo_client import HOURLY_VARS

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
WEATHER_DIR = PROJECT_ROOT / "data" / "raw" / "weather" / "archive"
OUT_FILE = PROJECT_ROOT / "data" / "interim" / "weather_validation.json"

COLUMNS = ["city", "time_utc", *HOURLY_VARS]

# Physically possible values (critical). None = no limit on that side.
PHYSICAL_LIMITS = {
    "temperature_2m": (-60, 60),  # deg C
    "relative_humidity_2m": (0, 100),  # %
    "precipitation": (0, None),  # mm per hour
    "wind_speed_10m": (0, None),  # km/h
    "wind_direction_10m": (0, 360),  # degrees
    "surface_pressure": (500, 1100),  # hPa
    "cloud_cover": (0, 100),  # %
}


def critical_row_suite(cities: list[str]) -> gx.ExpectationSuite:
    """Schema, keys and physically possible values."""
    e = gx.expectations
    checks = [
        e.ExpectTableColumnsToMatchSet(column_set=COLUMNS, exact_match=True),
        e.ExpectColumnValuesToBeInSet(column="city", value_set=cities),
        e.ExpectColumnValuesToNotBeNull(column="time_utc"),
        e.ExpectCompoundColumnsToBeUnique(column_list=["city", "time_utc"]),
    ]
    for col, (lo, hi) in PHYSICAL_LIMITS.items():
        checks.append(
            e.ExpectColumnValuesToBeBetween(column=col, min_value=lo, max_value=hi)
        )
    return gx.ExpectationSuite(name="weather_critical", expectations=checks)


def warning_row_suite(cfg: dict) -> gx.ExpectationSuite:
    """Missing values and values outside the plausible range for Indian cities."""
    e = gx.expectations
    checks = [
        # "mostly" = the fraction of rows that must pass for the check to pass.
        e.ExpectColumnValuesToNotBeNull(column=v, mostly=1 - cfg["max_missing_frac"])
        for v in HOURLY_VARS
    ]
    for col, (lo, hi) in cfg["plausible"].items():
        checks.append(
            e.ExpectColumnValuesToBeBetween(column=col, min_value=lo, max_value=hi)
        )
    return gx.ExpectationSuite(name="weather_warning", expectations=checks)


def completeness_suite() -> gx.ExpectationSuite:
    """Critical: every city has every hour of the configured period."""
    return gx.ExpectationSuite(
        name="weather_completeness",
        expectations=[
            gx.expectations.ExpectColumnValuesToBeInSet(
                column="complete", value_set=[True]
            ),
        ],
    )


def city_summary(
    df: pd.DataFrame, cities: list[str], start: str, end: str
) -> pd.DataFrame:
    """Per city: hours present vs expected, first and last hour."""
    first = pd.Timestamp(start)
    # Offsets instead of Timedelta arithmetic: avoids a NumPy 2.5 deprecation warning.
    last = pd.Timestamp(end) + pd.offsets.Hour(23)
    expected = len(pd.date_range(first, last, freq="h"))
    s = (
        df.groupby("city")["time_utc"]
        .agg(hours="nunique", first_utc="min", last_utc="max")
        .reindex(cities)  # a city with no rows at all appears with NaN
        .reset_index()
        .rename(columns={"index": "city"})
    )
    s["expected_hours"] = expected
    s["complete"] = (
        (s["hours"] == expected) & (s["first_utc"] == first) & (s["last_utc"] == last)
    )
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
                    "unexpected_percent": None if pct is None else round(float(pct), 4),
                    "examples": [
                        str(v) for v in (res.get("partial_unexpected_list") or [])[:5]
                    ],
                }
            )
    return failed


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logging.getLogger("great_expectations").setLevel(logging.WARNING)
    with open(PROJECT_ROOT / "params.yaml") as f:
        params = yaml.safe_load(f)
    cities = sorted(params["data"]["cities"])
    period = params["weather"]["archive"]
    cfg = params["weather"]["validation"]

    files = sorted(WEATHER_DIR.glob("*.parquet"))
    if not files:
        log.error("No weather files in %s", WEATHER_DIR)
        return 1
    start = time.perf_counter()
    df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)

    context = gx.get_context(mode="ephemeral")
    context.variables.progress_bars = ProgressBarsConfig(globally=False)
    batch_def = (
        context.data_sources.add_pandas("weather")
        .add_dataframe_asset(name="table")
        .add_batch_definition_whole_dataframe("whole_table")
    )

    def validate(frame: pd.DataFrame, suite: gx.ExpectationSuite) -> list[dict]:
        batch = batch_def.get_batch(batch_parameters={"dataframe": frame})
        return failed_checks(batch.validate(suite))

    summary = city_summary(df, cities, period["start_date"], period["end_date"])
    critical = {
        "rows": validate(df, critical_row_suite(cities)),
        "completeness": validate(summary, completeness_suite()),
    }
    warnings = {"rows": validate(df, warning_row_suite(cfg))}
    elapsed = time.perf_counter() - start
    ok = not any(critical.values())

    # Observed range per variable: useful for the data documentation.
    ranges = {
        v: {"min": float(df[v].min()), "max": float(df[v].max())} for v in HOURLY_VARS
    }
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(
        json.dumps(
            {
                "validated_at_utc": datetime.now(UTC).isoformat(),
                "settings": {**cfg, **period},
                "files": [f.name for f in files],
                "rows": len(df),
                "seconds": round(elapsed, 1),
                "rows_per_second": round(len(df) / elapsed) if elapsed else None,
                "critical_passed": ok,
                "critical": critical,
                "warnings": warnings,
                "incomplete_cities": summary.loc[~summary["complete"], "city"].tolist(),
                "observed_ranges": ranges,
            },
            indent=2,
            default=str,
        )
    )

    for name, problems in critical.items():
        for p in problems:
            log.error("CRITICAL %s: %s", name, p)
    for p in warnings["rows"]:
        log.warning("WARNING: %s", p)
    log.info(
        "Validated %d weather rows (%d files) in %.1f s: critical %s, %d warning(s)",
        len(df),
        len(files),
        elapsed,
        "PASSED" if ok else "FAILED",
        len(warnings["rows"]),
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
