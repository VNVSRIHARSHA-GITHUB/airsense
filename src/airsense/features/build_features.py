"""Feature engineering shared by training and serving (Stage 3.4, D37).

The same functions run on history (training) and on live data (serving), so both
compute features in exactly the same way (no training/serving skew):

- complete_hourly_grid: one row per city per hour, so "lag 24" means 24 HOURS back
  (not 24 rows back) even when hours are missing;
- pm_features:      from the city's 24-hour average PM2.5 only (D35: the live feed
                    has no hourly values): current level, lags, changes, rolling;
- weather_features: weather variables, wind direction as sin/cos, recent rain;
                    relabelled to the PM2.5 hour convention (see below);
- calendar_features: hour, weekday, month, weekend, Diwali flag, in Indian time.
                    Pure functions of time, so they are computed when the training
                    set is built and at serving time, not stored.

Time alignment (D38): PM2.5 rows are labelled at :30 UTC (the IST hour 16:00-17:00
is stored as 10:30 UTC); weather comes at whole UTC hours. Each PM2.5 hour is matched
with the weather at its middle: the 10:30 row gets the 11:00 weather. The weather
table is therefore relabelled by -30 minutes, so both tables join on time_utc.

Stored tables carry UTC-labelled timestamps (for_storage, D41), unlike the naive-UTC
tables of Stage 2, because Feast's DuckDB offline store needs them.

Usage (one DVC stage each):
  python build_features.py history  -> data/features/pm_history/, weather_history/
  python build_features.py live     -> data/features/pm_live/
"""

import argparse
import json
import logging
import shutil
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PROCESSED = PROJECT_ROOT / "data" / "processed"
WEATHER_DIR = PROJECT_ROOT / "data" / "raw" / "weather" / "archive"
FEATURES = PROJECT_ROOT / "data" / "features"
INTERIM = PROJECT_ROOT / "data" / "interim"

WEATHER_VARS = [
    "temperature_2m",
    "relative_humidity_2m",
    "precipitation",
    "wind_speed_10m",
    "surface_pressure",
    "cloud_cover",
]  # wind_direction_10m is replaced by its sin/cos


def complete_hourly_grid(df: pd.DataFrame) -> pd.DataFrame:
    """Give every city one row per hour between its first and last hour.

    Missing hours become rows with empty values, so shifting by k rows within a
    city means exactly k hours. Expects unique (city, time_utc) pairs.
    """
    parts = []
    for city, g in df.groupby("city", sort=True, observed=True):
        hours = pd.date_range(
            g["time_utc"].min(), g["time_utc"].max(), freq="h", name="time_utc"
        )
        part = g.drop(columns="city").set_index("time_utc").reindex(hours)
        parts.append(part.reset_index().assign(city=city))
    out = pd.concat(parts, ignore_index=True)
    return out[
        ["city", "time_utc", *[c for c in df.columns if c not in ("city", "time_utc")]]
    ]


def min_history_hours(cfg: dict) -> int:
    """Hours of past data the newest PM2.5 features need (longest look-back, D37).

    Serving only has recent live data; with at least this many hours before t,
    the features at t are identical to those computed on the full history.
    """
    return max(
        max(cfg["lags_hours"]), max(cfg["change_hours"]), cfg["rolling_hours"] - 1
    )


def pm_features(pm: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Lag, change and rolling features from the 24-hour average PM2.5.

    Every feature at hour t uses only values at t or earlier (no future data).
    """
    base = pm[["city", "time_utc", "pm25_24h", "n_stations_24h"]].copy()
    base["city"] = base["city"].astype(str)  # stored as a category in Parquet
    df = complete_hourly_grid(base)
    by_city = df.groupby("city", sort=False)["pm25_24h"]

    for k in cfg["lags_hours"]:
        df[f"pm25_lag_{k}h"] = by_city.shift(k)
    for k in cfg["change_hours"]:
        df[f"pm25_change_{k}h"] = df["pm25_24h"] - by_city.shift(k)

    w, m = cfg["rolling_hours"], cfg["rolling_min_hours"]
    roll = by_city.rolling(w, min_periods=m)  # window = hours t-(w-1) .. t
    df[f"pm25_mean_{w}h"] = roll.mean().reset_index(level=0, drop=True)
    df[f"pm25_max_{w}h"] = roll.max().reset_index(level=0, drop=True)
    return df


def weather_features(weather: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Weather features, relabelled to the PM2.5 hour they describe (D38)."""
    df = complete_hourly_grid(weather.assign(city=weather["city"].astype(str)))
    rad = np.deg2rad(df["wind_direction_10m"])
    df["wind_dir_sin"] = np.sin(rad)  # 0 deg and 360 deg give the same values
    df["wind_dir_cos"] = np.cos(rad)
    rw = cfg["rain_window_hours"]
    df[f"rain_{rw}h"] = (
        df.groupby("city", sort=False)["precipitation"]
        .rolling(rw, min_periods=rw)
        .sum()
        .reset_index(level=0, drop=True)
    )
    # Weather at 11:00 describes the PM2.5 hour labelled 10:30 (its middle).
    df["time_utc"] = df["time_utc"] - pd.to_timedelta(
        cfg["weather_offset_minutes"], unit="min"
    )
    return df[
        [
            "city",
            "time_utc",
            *WEATHER_VARS,
            "wind_dir_sin",
            "wind_dir_cos",
            f"rain_{rw}h",
        ]
    ]


def festival_days(cfg: dict) -> set:
    """All local dates within +/- festival_window_days of a Diwali date."""
    days = set()
    for d in pd.to_datetime(cfg["diwali_dates"]):
        for k in range(-cfg["festival_window_days"], cfg["festival_window_days"] + 1):
            days.add((d + pd.offsets.Day(k)).date())
    return days


def calendar_features(times_utc: pd.Series, cfg: dict) -> pd.DataFrame:
    """Calendar features of naive-UTC times, computed in Indian local time.

    Pollution follows local life (traffic peaks, cooking, firecrackers at night),
    and Diwali night crosses midnight UTC, so local time is the right clock.
    A row labelled 10:30 UTC is the IST hour starting at 16:00, so hour = 16.
    """
    local = times_utc.dt.tz_localize("UTC").dt.tz_convert(cfg["local_timezone"])
    out = pd.DataFrame(index=times_utc.index)
    out["hour"] = local.dt.hour
    out["day_of_week"] = local.dt.dayofweek  # Monday = 0
    out["month"] = local.dt.month
    out["is_weekend"] = (out["day_of_week"] >= 5).astype(int)
    out["is_festival"] = local.dt.date.isin(festival_days(cfg)).astype(int)
    return out


def for_storage(df: pd.DataFrame) -> pd.DataFrame:
    """Prepare a feature table for the Feast offline store (D41).

    Times get an explicit UTC label, in microseconds. Feast's DuckDB offline store
    reads a naive timestamp as LOCAL time of the computer (IST on the laptop, a
    5:30 shift), and cannot convert nanosecond UTC timestamps when materializing.
    The values are the same instants as before; only the label and unit change.
    """
    out = df.copy()
    out["city"] = out["city"].astype(str)
    out["time_utc"] = (
        out["time_utc"].dt.tz_localize("UTC").astype("datetime64[us, UTC]")
    )
    return out


def write_parquet(df: pd.DataFrame, out_dir: Path) -> None:
    """Replace out_dir with df, partitioned by city (full rebuild, never append).

    Fixed file names (part-0.parquet) instead of pyarrow's random ones: identical
    data then gives an identical folder, so DVC sees "unchanged" and does not
    re-run the stages after it (D43).
    """
    if out_dir.exists():
        shutil.rmtree(out_dir)
    df.to_parquet(
        out_dir,
        partition_cols=["city"],
        index=False,
        basename_template="part-{i}.parquet",
    )


def missing_fractions(df: pd.DataFrame) -> dict:
    """Fraction of empty values per feature column (a data-quality summary)."""
    cols = [c for c in df.columns if c not in ("city", "time_utc")]
    return {c: round(float(df[c].isna().mean()), 4) for c in cols}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", choices=["history", "live"])
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    with open(PROJECT_ROOT / "params.yaml") as f:
        params = yaml.safe_load(f)
    cfg = params["features"]

    start = time.perf_counter()
    if args.source == "history":
        pm = pd.read_parquet(PROCESSED / "city_hourly")
        weather = pd.concat(
            [pd.read_parquet(p) for p in sorted(WEATHER_DIR.glob("*.parquet"))],
            ignore_index=True,
        )
        tables = {
            "pm_history": pm_features(pm, cfg),
            "weather_history": weather_features(weather, cfg),
        }
    else:
        pm = pd.read_parquet(PROCESSED / "live_city_hourly")
        tables = {"pm_live": pm_features(pm, cfg)}

    for name, df in tables.items():
        write_parquet(for_storage(df), FEATURES / name)
    elapsed = time.perf_counter() - start

    metrics = {
        "built_at_utc": datetime.now(UTC).isoformat(),
        "source": args.source,
        "seconds": round(elapsed, 1),
        "tables": {
            name: {
                "rows": len(df),
                "cities": int(df["city"].nunique()),
                "first_utc": str(df["time_utc"].min()),
                "last_utc": str(df["time_utc"].max()),
                "missing_fraction": missing_fractions(df),
            }
            for name, df in tables.items()
        },
    }
    INTERIM.mkdir(parents=True, exist_ok=True)
    (INTERIM / f"features_{args.source}_metrics.json").write_text(
        json.dumps(metrics, indent=2)
    )
    rows = sum(len(df) for df in tables.values())
    log.info(
        "Built %s features: %s in %.1f s (%d rows/s)",
        args.source,
        ", ".join(f"{n} {len(d)} rows" for n, d in tables.items()),
        elapsed,
        rows / elapsed if elapsed else 0,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
