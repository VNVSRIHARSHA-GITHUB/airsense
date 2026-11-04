"""Validate raw CPCB station files with Great Expectations (Stage 2.2).

Two levels (design choice B):
- CRITICAL: a station failing any critical check is quarantined
  (excluded from processing).
- WARNING:  a station failing a warning check is kept; the issue is reported.
The run fails (exit code 1) only if a selected city is left with no valid
station, because its city-level series could then not be produced.

Output: data/interim/raw_validation.json (per-station results + valid list).
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
RAW_DIR = PROJECT_ROOT / "data" / "raw" / "cpcb"
OUT_FILE = PROJECT_ROOT / "data" / "interim" / "raw_validation.json"

PM = "PM2.5 (ug/m3)"
TIME_COL = "From Date"
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def build_suites(cfg: dict) -> tuple[gx.ExpectationSuite, gx.ExpectationSuite]:
    """Return the critical and warning expectation suites."""
    critical = gx.ExpectationSuite(
        name="cpcb_raw_critical",
        expectations=[
            gx.expectations.ExpectColumnToExist(column=TIME_COL),
            gx.expectations.ExpectColumnToExist(column=PM),
            gx.expectations.ExpectColumnValuesToNotBeNull(column=TIME_COL),
            gx.expectations.ExpectColumnValuesToMatchStrftimeFormat(
                column=TIME_COL, strftime_format=TIME_FORMAT
            ),
            gx.expectations.ExpectColumnValuesToBeUnique(column=TIME_COL),
            gx.expectations.ExpectColumnValuesToBeBetween(column=PM, min_value=0),
        ],
    )
    warning = gx.ExpectationSuite(
        name="cpcb_raw_warning",
        expectations=[
            # "mostly" = the fraction of rows that must pass for the check to pass.
            gx.expectations.ExpectColumnValuesToNotBeNull(
                column=PM, mostly=1 - cfg["max_missing_frac"]
            ),
            gx.expectations.ExpectColumnValuesToBeBetween(
                column=PM,
                max_value=cfg["cap_value"],
                strict_max=True,
                mostly=cfg["min_below_cap_frac"],
            ),
        ],
    )
    return critical, warning


def failed_checks(result) -> list[dict]:
    """Summarise only the failed expectations of a validation result."""
    failed = []
    for r in result.results:
        if not r.success:
            pct = r.result.get("unexpected_percent")
            failed.append(
                {
                    "expectation": r.expectation_config.type,
                    "column": r.expectation_config.kwargs.get("column"),
                    "unexpected_percent": None if pct is None else round(float(pct), 3),
                }
            )
    return failed


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    # Hide Great Expectations' own setup messages; keep its warnings and errors.
    logging.getLogger("great_expectations").setLevel(logging.WARNING)
    with open(PROJECT_ROOT / "params.yaml") as f:
        params = yaml.safe_load(f)
    cities = set(params["data"]["cities"])
    critical, warning = build_suites(params["validation"]["raw"])

    stations = pd.read_csv(RAW_DIR / "stations_info.csv")
    stations["city"] = stations["city"].str.strip()
    selected = stations[stations["city"].isin(cities)]
    unknown = cities - set(selected["city"])
    if unknown:
        log.error("Cities in params.yaml with no stations: %s", sorted(unknown))
        return 1

    # Ephemeral context: suites live in code (above), nothing is written to disk.
    context = gx.get_context(mode="ephemeral")
    context.variables.progress_bars = ProgressBarsConfig(globally=False)
    batch_def = (
        context.data_sources.add_pandas("cpcb_raw")
        .add_dataframe_asset(name="station_file")
        .add_batch_definition_whole_dataframe("whole_file")
    )

    start, total_rows, report = time.perf_counter(), 0, {}
    for code, city in zip(selected["file_name"], selected["city"], strict=True):
        path = RAW_DIR / f"{code}.csv"
        if not path.exists():
            report[code] = {
                "city": city,
                "status": "quarantined",
                "rows": 0,
                "failed_critical": [{"expectation": "file_exists"}],
                "warnings": [],
            }
            log.warning("%s (%s): file missing -> quarantined", code, city)
            continue

        df = pd.read_csv(path, low_memory=False)
        total_rows += len(df)
        batch = batch_def.get_batch(batch_parameters={"dataframe": df})
        crit = batch.validate(critical)
        # Warning checks only make sense if the critical columns exist.
        warn = failed_checks(batch.validate(warning)) if crit.success else []
        status = "valid" if crit.success else "quarantined"
        report[code] = {
            "city": city,
            "status": status,
            "rows": len(df),
            "failed_critical": failed_checks(crit),
            "warnings": warn,
        }
        if not crit.success:
            log.warning(
                "%s (%s): critical check failed -> quarantined: %s",
                code,
                city,
                report[code]["failed_critical"],
            )
        elif warn:
            log.info("%s (%s): warnings: %s", code, city, warn)

    elapsed = time.perf_counter() - start
    valid = sorted(c for c, r in report.items() if r["status"] == "valid")
    valid_per_city = {
        c: sum(1 for r in report.values() if r["city"] == c and r["status"] == "valid")
        for c in sorted(cities)
    }
    empty_cities = [c for c, n in valid_per_city.items() if n == 0]

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(
        json.dumps(
            {
                "validated_at_utc": datetime.now(UTC).isoformat(),
                "thresholds": params["validation"]["raw"],
                "stations_checked": len(report),
                "stations_valid": len(valid),
                "stations_quarantined": len(report) - len(valid),
                "rows_checked": total_rows,
                "seconds": round(elapsed, 1),
                "rows_per_second": round(total_rows / elapsed) if elapsed else None,
                "valid_per_city": valid_per_city,
                "valid_stations": valid,
                "stations": report,
            },
            indent=2,
        )
    )

    log.info(
        "Checked %d stations (%d rows) in %.1f s: %d valid, %d quarantined",
        len(report),
        total_rows,
        elapsed,
        len(valid),
        len(report) - len(valid),
    )
    if empty_cities:
        log.error("No valid station left for: %s", empty_cities)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
