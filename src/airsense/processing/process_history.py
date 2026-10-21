"""Clean the raw CPCB history and build city-level 24-hour average PM2.5 (Stage 2.3).

Reads only the stations that passed raw validation (data/interim/raw_validation.json)
and, with Apache Spark in local mode:
1. parses the timestamps (Indian time in the source) and converts them to UTC;
2. removes readings inside "stuck sensor" runs (>= N identical consecutive values);
3. flags readings at the instrument cap (kept, but marked);
4. computes each station's trailing 24-hour average PM2.5, only when enough hours
   in the window are valid (CPCB's minimum-data rule for the 24-hour average);
5. averages the stations of each city, hour by hour.

The station average is computed first and the city average second, because the
live feed delivers station-level 24-hour averages: training and live data are then
built in the same order (no training-serving skew).

Outputs:
  data/processed/station_hourly/          Parquet, one row per station-hour
  data/processed/city_hourly/             Parquet, one row per city-hour (the target)
  data/interim/process_history_metrics.json
"""

import json
import logging
import os
import sys
import time
from datetime import UTC, datetime
from functools import reduce
from pathlib import Path

import yaml
from pyspark.sql import DataFrame, SparkSession, Window
from pyspark.sql import functions as F

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
RAW_DIR = PROJECT_ROOT / "data" / "raw" / "cpcb"
VALIDATION_FILE = PROJECT_ROOT / "data" / "interim" / "raw_validation.json"
STATION_OUT = PROJECT_ROOT / "data" / "processed" / "station_hourly"
CITY_OUT = PROJECT_ROOT / "data" / "processed" / "city_hourly"
METRICS_FILE = PROJECT_ROOT / "data" / "interim" / "process_history_metrics.json"

PM_COL = "PM2.5 (ug/m3)"
TIME_COL = "From Date"
TIME_FORMAT = "yyyy-MM-dd HH:mm:ss"  # Spark's datetime pattern syntax
UTC_FMT = "yyyy-MM-dd'T'HH:mm:ss'Z'"
SOURCE_TZ = "Asia/Kolkata"  # Kaggle timestamps are Indian local time (EDA)


def build_spark(cfg: dict) -> SparkSession:
    """Start Spark with the settings from params.yaml (processing section)."""
    spark = (
        SparkSession.builder.appName("airsense-process-history")
        .master(cfg["spark_master"])
        .config("spark.driver.memory", cfg["driver_memory"])
        .config("spark.sql.shuffle.partitions", str(cfg["shuffle_partitions"]))
        .config("spark.sql.session.timeZone", "UTC")  # store and show times in UTC
        .config("spark.ui.showConsoleProgress", "false")  # no progress bars in logs
        .getOrCreate()
    )
    spark.sparkContext.setLogLevel("WARN")
    return spark


def read_station(spark: SparkSession, code: str) -> DataFrame:
    """Read one station file; keep only the time and PM2.5 columns (by name)."""
    df = spark.read.option("header", True).csv(str(RAW_DIR / f"{code}.csv"))
    # Backticks: the column names contain a space and a dot.
    return df.select(
        F.lit(code).alias("station"),
        F.col(f"`{TIME_COL}`").alias("time_raw"),
        F.col(f"`{PM_COL}`").cast("double").alias("pm25"),
    )


def remove_stuck_runs(df: DataFrame, min_run: int) -> DataFrame:
    """Blank out readings inside runs of >= min_run identical consecutive hourly values.

    A run breaks when the value changes, when a value is missing, or when the
    next row is not exactly one hour later.
    """
    w = Window.partitionBy("station").orderBy("time_utc")
    prev_val = F.lag("pm25").over(w)
    prev_time = F.lag("time_utc").over(w)
    starts_run = (
        F.col("pm25").isNull()
        | prev_val.isNull()
        | (F.col("pm25") != prev_val)
        | ((F.col("time_utc").cast("long") - prev_time.cast("long")) != 3600)
    )
    df = df.withColumn("_new_run", F.when(starts_run, 1).otherwise(0)).withColumn(
        "_run_id", F.sum("_new_run").over(w)
    )
    run_len = F.count(F.lit(1)).over(Window.partitionBy("station", "_run_id"))
    is_stuck = F.col("pm25").isNotNull() & (run_len >= min_run)
    return (
        df.withColumn("is_stuck", is_stuck)
        .withColumn(
            "pm25", F.when(F.col("is_stuck"), F.lit(None)).otherwise(F.col("pm25"))
        )
        .drop("_new_run", "_run_id")
    )


def add_rolling_average(df: DataFrame, window_h: int, min_valid: int) -> DataFrame:
    """Trailing average over the last window_h hours (current hour included).

    The window is time-based (rangeBetween on seconds), so missing rows can never
    stretch it beyond window_h hours. The average is kept only if at least
    min_valid hours in the window have a value.
    """
    w = (
        Window.partitionBy("station")
        .orderBy(F.col("time_utc").cast("long"))
        .rangeBetween(-(window_h - 1) * 3600, 0)
    )
    return df.withColumn("n_valid_24h", F.count("pm25").over(w)).withColumn(
        "pm25_24h", F.when(F.col("n_valid_24h") >= min_valid, F.avg("pm25").over(w))
    )


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    os.environ.setdefault(
        "PYSPARK_PYTHON", sys.executable
    )  # Spark uses this venv's Python
    with open(PROJECT_ROOT / "params.yaml") as f:
        params = yaml.safe_load(f)
    cfg = params["processing"]
    cap_value = params["validation"]["raw"]["cap_value"]
    cities = sorted(params["data"]["cities"])

    if not VALIDATION_FILE.exists():
        log.error("%s not found; run validate_raw.py first", VALIDATION_FILE)
        return 1
    report = json.loads(VALIDATION_FILE.read_text())
    valid = {c: report["stations"][c]["city"] for c in report["valid_stations"]}
    log.info("%d valid stations from raw validation", len(valid))

    t0 = time.perf_counter()
    spark = build_spark(cfg)
    try:
        # 1. Read the valid stations and attach their city.
        raw = reduce(
            DataFrame.unionByName, [read_station(spark, c) for c in sorted(valid)]
        )
        city_map = spark.createDataFrame(list(valid.items()), ["station", "city"])

        # 2. Timestamps: parse as Indian local time, convert to UTC.
        df = (
            raw.join(F.broadcast(city_map), "station")
            .withColumn(
                "time_utc",
                F.to_utc_timestamp(F.to_timestamp("time_raw", TIME_FORMAT), SOURCE_TZ),
            )
            .drop("time_raw")
            .dropna(subset=["time_utc"])
            .dropDuplicates(["station", "time_utc"])
        )

        # 3-4. Stuck sensors, cap flag, 24-hour station average.
        df = remove_stuck_runs(df, cfg["stuck_run_hours"])
        df = df.withColumn(
            "is_capped", F.coalesce(F.col("pm25") >= cap_value, F.lit(False))
        )
        df = add_rolling_average(df, cfg["avg_window_hours"], cfg["min_valid_hours"])

        (
            df.select(
                "city",
                "station",
                "time_utc",
                "pm25",
                "is_stuck",
                "is_capped",
                "n_valid_24h",
                "pm25_24h",
            )
            .write.mode("overwrite")
            .partitionBy("city")
            .parquet(str(STATION_OUT))
        )
        t_station = time.perf_counter() - t0
        log.info("Station-level data written in %.1f s", t_station)

        # 5. City level: mean of the stations' 24-hour averages, hour by hour.
        stations = spark.read.parquet(str(STATION_OUT))
        city = stations.groupBy("city", "time_utc").agg(
            F.avg("pm25_24h").alias("pm25_24h"),
            F.count("pm25_24h").alias("n_stations_24h"),
            F.avg("pm25").alias("pm25_1h"),
            F.count("pm25").alias("n_stations_1h"),
            F.sum(F.col("is_capped").cast("int")).alias("n_capped"),
        )
        city.write.mode("overwrite").partitionBy("city").parquet(str(CITY_OUT))
        t_total = time.perf_counter() - t0
        log.info(
            "City-level data written in %.1f s (total %.1f s)",
            t_total - t_station,
            t_total,
        )

        # Metrics: read back the (small, columnar) outputs.
        s = (
            stations.agg(
                F.count(F.lit(1)).alias("rows"),
                F.count("pm25").alias("valid_hourly"),
                F.sum(F.col("is_stuck").cast("int")).alias("stuck_removed"),
                F.sum(F.col("is_capped").cast("int")).alias("capped_flagged"),
                F.count("pm25_24h").alias("valid_24h"),
            )
            .first()
            .asDict()
        )
        per_city = {
            r["city"]: {
                "first_utc": r["first"],
                "last_utc": r["last"],
                "city_hours": r["hours"],
                "pct_hours_with_24h_avg": round(100 * r["with_avg"] / r["hours"], 1),
                "mean_pm25_24h": round(r["mean"], 1) if r["mean"] else None,
            }
            for r in spark.read.parquet(str(CITY_OUT))
            .groupBy("city")
            .agg(
                # Format inside Spark (session time zone = UTC): Python would
                # otherwise convert timestamps to the laptop's local time.
                F.date_format(F.min("time_utc"), UTC_FMT).alias("first"),
                F.date_format(F.max("time_utc"), UTC_FMT).alias("last"),
                F.count(F.lit(1)).alias("hours"),
                F.count("pm25_24h").alias("with_avg"),
                F.avg("pm25_24h").alias("mean"),
            )
            .collect()
        }
    finally:
        spark.stop()

    metrics = {
        "processed_at_utc": datetime.now(UTC).isoformat(),
        "settings": {**cfg, "cap_value": cap_value},
        "stations": len(valid),
        "station_rows": s["rows"],
        "valid_hourly_values": s["valid_hourly"],
        "stuck_readings_removed": s["stuck_removed"],
        "capped_readings_flagged": s["capped_flagged"],
        "station_hours_with_24h_avg": s["valid_24h"],
        "seconds_total": round(t_total, 1),
        "rows_per_second": round(s["rows"] / t_total),
        "cities": per_city,
    }
    METRICS_FILE.write_text(json.dumps(metrics, indent=2))
    log.info(
        "%d station-hours, %d stuck readings removed, %d capped flagged, %.0f rows/s",
        s["rows"],
        s["stuck_removed"],
        s["capped_flagged"],
        metrics["rows_per_second"],
    )

    missing = [c for c in cities if c not in per_city]
    if missing:
        log.error("No processed data for: %s", missing)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
