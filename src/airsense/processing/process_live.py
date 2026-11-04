"""Process the live data.gov.in snapshots into city-level 24-hour PM2.5 (Stage 2.4).

The live feed already reports each station's 24-hour average (avg_value), so
this step only has to clean, de-duplicate and aggregate it, and produce exactly the
same columns as the processed history (data/processed/city_hourly) so that live
and training data can be compared and used without special cases.

Steps:
1. read every snapshot CSV; keep PM2.5 records of the configured cities;
2. convert last_update (Indian time) to UTC and apply the hour-label convention
   (params.yaml -> live.time_offset_hours);
3. keep one row per (station, hour): overlapping snapshots repeat readings;
4. average the stations of each city, hour by hour.

The outputs are rebuilt from all snapshots on every run (idempotent: running it
twice gives the same result).

Outputs:
  data/processed/live_station_hourly/   Parquet, one row per station-hour
  data/processed/live_city_hourly/      Parquet, same columns as city_hourly
  data/interim/process_live_metrics.json
"""

import json
import logging
import shutil
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import yaml

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
LIVE_DIR = PROJECT_ROOT / "data" / "raw" / "live" / "datagov"
STATION_OUT = PROJECT_ROOT / "data" / "processed" / "live_station_hourly"
CITY_OUT = PROJECT_ROOT / "data" / "processed" / "live_city_hourly"
METRICS_FILE = PROJECT_ROOT / "data" / "interim" / "process_live_metrics.json"

# The feed's own label for PM2.5. AirSense is built for PM2.5 only (features, AQI
# bands, model, advice), so this is a fixed value, not a setting.
POLLUTANT = "PM2.5"
TIME_FORMAT = "%d-%m-%Y %H:%M:%S"  # data.gov.in last_update, e.g. 20-10-2026 16:00:00
SOURCE_TZ = "Asia/Kolkata"
VALUE_COLS = ["min_value", "max_value", "avg_value"]


def read_snapshots(live_dir: Path) -> tuple[pd.DataFrame, list[int]]:
    """Read all snapshot CSVs as text; also return the record count of each."""
    files = sorted(live_dir.glob("*/*.csv"))
    frames = [pd.read_csv(f, dtype=str) for f in files]
    return (
        pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(),
        [len(f) for f in frames],
    )


def to_station_hours(
    raw: pd.DataFrame, cities: list[str], offset_h: int
) -> pd.DataFrame:
    """Clean the snapshot records into one row per station-hour (UTC)."""
    df = raw.copy()
    for col in ["state", "city", "station", "pollutant_id"]:
        df[col] = df[col].str.strip()
    df = df[(df["pollutant_id"] == POLLUTANT) & df["city"].isin(cities)].copy()

    local = pd.to_datetime(df["last_update"], format=TIME_FORMAT, errors="coerce")
    df["time_utc"] = (
        local.dt.tz_localize(SOURCE_TZ)
        .dt.tz_convert("UTC")
        .dt.tz_localize(None)  # naive UTC, like the history
        + pd.to_timedelta(offset_h, unit="h")
    )
    for col in [*VALUE_COLS, "latitude", "longitude"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["time_utc", "avg_value"])


def deduplicate(df: pd.DataFrame) -> tuple[pd.DataFrame, int, int]:
    """Keep the most recently fetched row per (station, hour).

    Returns the de-duplicated rows, the number of repeats removed, and the number
    of station-hours whose repeats disagreed (a data-quality signal: the source
    revised an already-published value).
    """
    key = ["station", "time_utc"]
    n_versions = df.groupby(key)["avg_value"].transform("nunique")
    conflicts = int(df.loc[n_versions > 1, key].drop_duplicates().shape[0])
    out = df.sort_values("fetched_at_utc").drop_duplicates(key, keep="last")
    return out, len(df) - len(out), conflicts


def to_city_hours(st: pd.DataFrame) -> pd.DataFrame:
    """Mean of the stations' 24-hour averages per city-hour (columns as city_hourly)."""
    city = (
        st.groupby(["city", "time_utc"])
        .agg(pm25_24h=("pm25_24h", "mean"), n_stations_24h=("pm25_24h", "count"))
        .reset_index()
    )
    # Not available in the live feed (no hourly values); kept so both tables match.
    city["pm25_1h"] = float("nan")
    city["n_stations_1h"] = 0
    city["n_capped"] = 0
    return city[
        [
            "city",
            "time_utc",
            "pm25_24h",
            "n_stations_24h",
            "pm25_1h",
            "n_stations_1h",
            "n_capped",
        ]
    ]


def write_parquet(df: pd.DataFrame, out_dir: Path) -> None:
    """Replace out_dir with df, partitioned by city (same layout as the history)."""
    if out_dir.exists():
        shutil.rmtree(out_dir)  # full rebuild: never append to old files
    df.to_parquet(out_dir, partition_cols=["city"], index=False)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    with open(PROJECT_ROOT / "params.yaml") as f:
        params = yaml.safe_load(f)
    cities = list(params["data"]["cities"])
    offset_h = params["live"]["time_offset_hours"]
    min_records = params["sources"]["datagov"].get("min_records", 200)

    start = time.perf_counter()
    raw, sizes = read_snapshots(LIVE_DIR)
    if raw.empty:
        log.error("No snapshots found in %s", LIVE_DIR)
        return 1

    rows = to_station_hours(raw, cities, offset_h)
    rows, repeats, conflicts = deduplicate(rows)
    stations = rows.rename(
        columns={
            "avg_value": "pm25_24h",
            "min_value": "pm25_min_24h",
            "max_value": "pm25_max_24h",
        }
    )[
        [
            "city",
            "state",
            "station",
            "time_utc",
            "pm25_24h",
            "pm25_min_24h",
            "pm25_max_24h",
            "latitude",
            "longitude",
            "fetched_at_utc",
        ]
    ].sort_values(["city", "station", "time_utc"])
    city = to_city_hours(stations)
    write_parquet(stations, STATION_OUT)
    write_parquet(city, CITY_OUT)
    elapsed = time.perf_counter() - start

    per_city = {
        c: {
            "hours": len(g),
            "first_utc": g["time_utc"].min().isoformat() + "Z",
            "last_utc": g["time_utc"].max().isoformat() + "Z",
            "mean_stations": round(float(g["n_stations_24h"].mean()), 1),
            "mean_pm25_24h": round(float(g["pm25_24h"].mean()), 1),
        }
        for c, g in city.groupby("city")
    }
    missing = [c for c in cities if c not in per_city]

    metrics = {
        "processed_at_utc": datetime.now(UTC).isoformat(),
        "time_offset_hours": offset_h,
        "snapshots": len(sizes),
        "partial_snapshots": sum(n < min_records for n in sizes),
        "raw_records": len(raw),
        "repeats_removed": repeats,
        "conflicting_repeats": conflicts,
        "station_hours": len(stations),
        "city_hours": len(city),
        "seconds": round(elapsed, 2),
        "cities_without_live_data": missing,
        "cities": per_city,
    }
    METRICS_FILE.parent.mkdir(parents=True, exist_ok=True)
    METRICS_FILE.write_text(json.dumps(metrics, indent=2))
    log.info(
        "%d snapshots -> %d station-hours, %d city-hours in %.1f s "
        "(%d repeats removed, %d conflicting)",
        len(sizes),
        len(stations),
        len(city),
        elapsed,
        repeats,
        conflicts,
    )
    if missing:
        # Warning, not failure: a city can be briefly absent from the feed.
        log.warning("No live data for: %s", missing)
    return 0


if __name__ == "__main__":
    sys.exit(main())
