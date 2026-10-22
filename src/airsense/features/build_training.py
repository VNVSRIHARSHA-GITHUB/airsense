"""Build the training table through Feast (Stage 3.6, D31, D42).

1. Entity rows: every (city, hour) of the PM2.5 history from features.train_start on.
2. Feast point-in-time join (training_features service, DuckDB offline store): the
   PM2.5 features and the weather "now" for each row, as they were at that hour.
3. Horizon expansion (one model with the horizon as a feature, D31): each row is
   repeated for h = 1 .. forecast.horizon_hours, with
     - target          = pm25_24h at t + h,
     - <weather>_target = weather at t + h (archive here, forecast at serving, D34),
     - calendar of t + h in Indian time (D39),
     - region (static, from params.yaml).
   Rows whose target is missing are dropped (no imputation, D40).

The weather at t + h comes from the same Feast result: every t + h is itself an
entity row, so its weather was already retrieved point-in-time.

Output: data/training/h01.parquet .. h24.parquet (one file per horizon, float32)
        data/interim/training_metrics.json
"""

import json
import logging
import resource
import shutil
import sys
import time
import warnings
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import yaml
from feast import FeatureStore

from airsense.features.build_features import calendar_features

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
REPO = PROJECT_ROOT / "feature_repo"
PM_HISTORY = PROJECT_ROOT / "data" / "features" / "pm_history"
OUT_DIR = PROJECT_ROOT / "data" / "training"
METRICS_FILE = PROJECT_ROOT / "data" / "interim" / "training_metrics.json"


def entity_rows(pm: pd.DataFrame, start: str) -> pd.DataFrame:
    """One row per (city, hour) from `start` on: the rows Feast fills with features."""
    rows = pm.loc[pm["time_utc"] >= pd.Timestamp(start, tz="UTC"), ["city", "time_utc"]]
    return rows.assign(city=rows["city"].astype(str)).rename(
        columns={"time_utc": "event_timestamp"}
    )


def expand_horizon(
    base: pd.DataFrame,
    h: int,
    weather_cols: list[str],
    regions: dict[str, str],
    cal_cfg: dict,
) -> pd.DataFrame:
    """Training rows for one horizon h from the Feast result `base`.

    base: one row per (city, time_utc) with the PM2.5 features and the weather at
    that hour. Features stay at t; target, target weather and calendar are at t + h.
    """
    now = base.rename(columns={c: f"{c}_now" for c in weather_cols})
    target = base[["city", "time_utc", "pm25_24h", *weather_cols]].rename(
        columns={
            "time_utc": "target_time_utc",
            "pm25_24h": "target",
            **{c: f"{c}_target" for c in weather_cols},
        }
    )
    part = now.assign(
        horizon=h,
        target_time_utc=now["time_utc"] + pd.to_timedelta(h, unit="h"),
    ).merge(target, on=["city", "target_time_utc"], how="inner")
    part = part[part["target"].notna()].reset_index(drop=True)

    cal = calendar_features(part["target_time_utc"].dt.tz_convert(None), cal_cfg)
    part = pd.concat([part, cal.reset_index(drop=True)], axis=1)
    part["region"] = part["city"].map(regions)
    return part


def compact(df: pd.DataFrame) -> pd.DataFrame:
    """float32 / int16 numbers and microsecond UTC times: about half the memory."""
    out = df.copy()
    for c in out.columns:
        kind = out[c].dtype
        if kind == np.float64:
            out[c] = out[c].astype(np.float32)
        elif kind in (np.int64, np.int32):
            out[c] = out[c].astype(np.int16)  # horizon, hour, weekday, month, flags
        elif isinstance(kind, pd.DatetimeTZDtype):
            out[c] = out[c].astype("datetime64[us, UTC]")  # same format as D41
    return out


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logging.getLogger("feast").setLevel(logging.WARNING)
    warnings.filterwarnings("ignore", category=DeprecationWarning)
    with open(PROJECT_ROOT / "params.yaml") as f:
        params = yaml.safe_load(f)
    cfg = params["features"]
    horizons = range(1, params["forecast"]["horizon_hours"] + 1)
    regions = {c: v["region"] for c, v in params["data"]["cities"].items()}

    start = time.perf_counter()
    store = FeatureStore(repo_path=str(REPO))
    weather_cols = [f.name for f in store.get_feature_view("weather_history").features]
    entities = entity_rows(
        pd.read_parquet(PM_HISTORY, columns=["city", "time_utc"]), cfg["train_start"]
    )
    log.info("Feast point-in-time join for %d entity rows ...", len(entities))
    base = (
        store.get_historical_features(
            entity_df=entities,
            features=store.get_feature_service("training_features"),
            full_feature_names=False,
        )
        .to_df()
        .rename(columns={"event_timestamp": "time_utc"})
    )
    join_s = time.perf_counter() - start

    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)  # full rebuild, never append
    OUT_DIR.mkdir(parents=True)
    per_h = {}
    for h in horizons:
        part = compact(expand_horizon(base, h, weather_cols, regions, cfg))
        part.to_parquet(OUT_DIR / f"h{h:02d}.parquet", index=False)
        per_h[h] = len(part)
    elapsed = time.perf_counter() - start
    total = sum(per_h.values())
    columns = list(part.columns)

    METRICS_FILE.parent.mkdir(parents=True, exist_ok=True)
    METRICS_FILE.write_text(
        json.dumps(
            {
                "built_at_utc": datetime.now(UTC).isoformat(),
                "train_start": cfg["train_start"],
                "entity_rows": len(entities),
                "entity_rows_with_target": int(base["pm25_24h"].notna().sum()),
                "feast_join_seconds": round(join_s, 1),
                "seconds": round(elapsed, 1),
                "rows": total,
                "rows_per_second": round(total / elapsed) if elapsed else None,
                "peak_memory_mb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                // 1024,
                "first_time_utc": str(base["time_utc"].min()),
                "last_time_utc": str(base["time_utc"].max()),
                "n_columns": len(columns),
                "columns": columns,
                "rows_per_horizon": per_h,
            },
            indent=2,
        )
    )
    log.info(
        "Training table: %d rows x %d columns (%d horizons) in %.1f s "
        "(Feast join %.1f s)",
        total,
        len(columns),
        len(per_h),
        elapsed,
        join_s,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
