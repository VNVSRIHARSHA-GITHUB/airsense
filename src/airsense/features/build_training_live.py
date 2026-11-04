"""Training rows from the collected live data (Stage 10).

The history table (build_training.py) ends in 2023. This adds the hours collected
live since October 2026, built the same way, so a retrain can learn current
conditions:

  PM2.5    data/features/pm_live (the live feature table, same columns as pm_history)
  weather  data/raw/live/openmeteo snapshots: only the hours already OBSERVED when
           a snapshot was fetched (newest fetch wins), turned into features by the
           same weather_features() as the history. Like the archive weather used
           for the history, this is measured weather, not a forecast.
  rows     expand_horizon() from build_training.py: features at t, target = pm25_24h
           at t + h, weather at t + h, calendar of t + h, region. Rows without a
           target or with incomplete weather are dropped (no imputation).

Output: data/training_live/h01.parquet .. h24.parquet (same columns as
        data/training), data/interim/training_live_metrics.json.
train.py adds these rows to the training split with a higher weight
(train.live_weight_share) and keeps the newest days aside to report the error on
recent live data (train.live_holdout_days).
"""

import json
import logging
import shutil
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from airsense.common.settings import PROJECT_ROOT, load_params
from airsense.features.build_features import weather_features
from airsense.features.build_training import compact, expand_horizon

log = logging.getLogger(__name__)

PM_LIVE = PROJECT_ROOT / "data" / "features" / "pm_live"
WEATHER_SNAPSHOTS = PROJECT_ROOT / "data" / "raw" / "live" / "openmeteo"
OUT_DIR = PROJECT_ROOT / "data" / "training_live"
METRICS_FILE = PROJECT_ROOT / "data" / "interim" / "training_live_metrics.json"


def weather_columns(cfg: dict) -> list[str]:
    """The weather features of the training table (as in feature_repo/features.py)."""
    return [
        "temperature_2m",
        "relative_humidity_2m",
        "precipitation",
        "wind_speed_10m",
        "surface_pressure",
        "cloud_cover",
        "wind_dir_sin",
        "wind_dir_cos",
        f"rain_{cfg['rain_window_hours']}h",
    ]


def observed_weather(folder: Path, cfg: dict) -> pd.DataFrame:
    """Weather features per city and hour from the saved Open-Meteo snapshots.

    Each snapshot also holds the past days; only hours up to its fetch time are
    observed (later ones are forecasts) and kept. Of several snapshots covering
    the same hour, the newest COMPLETE one is used: a snapshot's first hours have
    no 24-hour rain total (its window starts before the snapshot), and the newest
    snapshot covering an old hour is exactly one where it is among the first.
    """
    frames = []
    for f in sorted(folder.glob("*/*.csv")):
        raw = pd.read_csv(f)
        raw["time_utc"] = pd.to_datetime(raw["time_utc"], utc=True)
        fetched = pd.to_datetime(raw.pop("fetched_at_utc"), utc=True).iloc[0]
        w = weather_features(raw, cfg)
        w["time_utc"] = pd.to_datetime(w["time_utc"], utc=True)
        frames.append(w[w["time_utc"] <= fetched].assign(fetched=fetched))
    cols = weather_columns(cfg)
    if not frames:
        return pd.DataFrame(columns=["city", "time_utc", *cols])
    return (
        pd.concat(frames, ignore_index=True)
        .dropna(subset=cols)
        .sort_values("fetched")
        .drop_duplicates(["city", "time_utc"], keep="last")
        .drop(columns="fetched")
        .reset_index(drop=True)
    )


def live_base(pm: pd.DataFrame, weather: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """One row per city and hour with the PM2.5 features and complete weather."""
    cols = weather_columns(cfg)
    pm = pm.assign(
        city=pm["city"].astype(str), time_utc=pd.to_datetime(pm["time_utc"], utc=True)
    )
    w = weather[["city", "time_utc", *cols]].dropna(subset=cols)
    return pm.merge(w, on=["city", "time_utc"], how="inner").sort_values(
        ["city", "time_utc"], ignore_index=True
    )


def build(base: pd.DataFrame, params: dict) -> dict[int, pd.DataFrame]:
    cfg = params["features"]
    regions = {c: v["region"] for c, v in params["data"]["cities"].items()}
    cols = weather_columns(cfg)
    out = {}
    for h in range(1, params["forecast"]["horizon_hours"] + 1):
        part = expand_horizon(base, h, cols, regions, cfg)
        out[h] = compact(part.dropna(subset=[f"{c}_target" for c in cols]))
    return out


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    params = load_params()
    start = time.perf_counter()
    pm = pd.read_parquet(PM_LIVE)
    weather = observed_weather(WEATHER_SNAPSHOTS, params["features"])
    base = live_base(pm, weather, params["features"])
    parts = build(base, params)
    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)  # full rebuild, never append
    OUT_DIR.mkdir(parents=True)
    for h, part in parts.items():
        part.to_parquet(OUT_DIR / f"h{h:02d}.parquet", index=False)
    rows = {h: len(p) for h, p in parts.items()}
    total = sum(rows.values())
    METRICS_FILE.parent.mkdir(parents=True, exist_ok=True)
    METRICS_FILE.write_text(
        json.dumps(
            {
                "built_at_utc": datetime.now(UTC).isoformat(),
                "live_city_hours": len(pm),
                "city_hours_with_weather": len(base),
                "first_time_utc": str(base["time_utc"].min()) if len(base) else None,
                "last_time_utc": str(base["time_utc"].max()) if len(base) else None,
                "rows": total,
                "rows_per_horizon": rows,
                "seconds": round(time.perf_counter() - start, 1),
            },
            indent=2,
        )
    )
    log.info(
        "Live training rows: %d (%d city-hours with weather, of %d live)",
        total,
        len(base),
        len(pm),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
