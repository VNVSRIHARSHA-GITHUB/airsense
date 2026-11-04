"""Data drift: do the live inputs still look like the training data? (Stage 10)

Run once a day by airsense_monitor. For each checked input, compares the values
of the last `drift_days` days of live data with the training rows of the same
months (`drift_months`, e.g. Oct-Nov), using the Population Stability Index:

    PSI = sum over bins of (live% - train%) * ln(live% / train%)

with 10 bins cut at the training data's deciles. PSI < psi_moderate: stable;
up to psi_drift: moderate change; above: drift. Same months only, because every
season differs from the yearly average: comparing November with all of
2018-2022 would always report "drift".

Inputs:
  PM2.5   data/features/pm_live (built by the live pipeline)
  weather data/raw/live/openmeteo snapshots, the hours already observed when
          fetched, turned into features by the same code as for training
          (observed_weather, shared with build_training_live)
  reference data/training/h01.parquet (one horizon: one row per city and hour)

Writes data/monitoring/reports/drift.json; `drift` is true when any input's PSI
is above psi_drift (needs at least `min_live_rows` live values for that input).
"""

import logging
import sys
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from airsense.common.settings import PROJECT_ROOT, load_params
from airsense.features.build_training_live import observed_weather
from airsense.monitoring.evaluate import write_json

log = logging.getLogger(__name__)

PM_LIVE = PROJECT_ROOT / "data" / "features" / "pm_live"
WEATHER_SNAPSHOTS = PROJECT_ROOT / "data" / "raw" / "live" / "openmeteo"
REFERENCE = PROJECT_ROOT / "data" / "training" / "h01.parquet"
REPORT = PROJECT_ROOT / "data" / "monitoring" / "reports" / "drift.json"
IST = "Asia/Kolkata"

# input name -> column in the training table ("now" weather is the observed hour)
REF_COLUMN = {
    "pm25_24h": "pm25_24h",
    "pm25_change_24h": "pm25_change_24h",
    "temperature_2m": "temperature_2m_now",
    "relative_humidity_2m": "relative_humidity_2m_now",
    "wind_speed_10m": "wind_speed_10m_now",
    "rain_24h": "rain_24h_now",
}


def psi(reference: np.ndarray, live: np.ndarray, bins: int = 10) -> float:
    """Population Stability Index of `live` against `reference` (decile bins)."""
    reference = reference[np.isfinite(reference)]
    live = live[np.isfinite(live)]
    edges = np.unique(np.quantile(reference, np.linspace(0, 1, bins + 1)))
    if len(edges) < 3:  # (almost) constant in training, e.g. rain: no deciles
        edges = np.unique(np.concatenate([edges, [edges[0] + 1e-9]]))
    edges[0], edges[-1] = -np.inf, np.inf
    eps = 1e-4  # an empty bin would give ln(0)
    ref_p = np.histogram(reference, edges)[0] / len(reference) + eps
    live_p = np.histogram(live, edges)[0] / len(live) + eps
    return float(np.sum((live_p - ref_p) * np.log(live_p / ref_p)))


def load_reference(months: list[int], path=REFERENCE) -> pd.DataFrame:
    present = set(pq.read_schema(path).names)  # an input missing in training is skipped
    cols = ["time_utc", *(c for c in REF_COLUMN.values() if c in present)]
    ref = pd.read_parquet(path, columns=cols)
    month = pd.to_datetime(ref["time_utc"], utc=True).dt.tz_convert(IST).dt.month
    return ref[month.isin(months)].rename(columns={v: k for k, v in REF_COLUMN.items()})


def load_live(since: datetime, cfg_features: dict) -> pd.DataFrame:
    """Live values per city and hour since `since`: PM2.5 features and observed weather."""
    present = set(pq.read_schema(next(PM_LIVE.rglob("*.parquet"))).names)
    pm = pd.read_parquet(
        PM_LIVE,
        columns=[
            "city",
            "time_utc",
            *(c for c in ("pm25_24h", "pm25_change_24h") if c in present),
        ],
    )
    pm["city"] = pm["city"].astype(str)
    pm["time_utc"] = pd.to_datetime(pm["time_utc"], utc=True)
    pm = pm[pm["time_utc"] >= since]
    weather = observed_weather(WEATHER_SNAPSHOTS, cfg_features)
    if weather.empty:
        return pm
    weather = weather[weather["time_utc"] >= since]
    rw = cfg_features["rain_window_hours"]
    weather = weather.rename(columns={f"rain_{rw}h": "rain_24h"})
    keep = [
        "city",
        "time_utc",
        "temperature_2m",
        "relative_humidity_2m",
        "wind_speed_10m",
        "rain_24h",
    ]
    return pm.merge(weather[keep], on=["city", "time_utc"], how="outer")


def drift_report(
    ref: pd.DataFrame, live: pd.DataFrame, cfg: dict, now: datetime
) -> dict:
    inputs = {}
    for name in REF_COLUMN:
        lv = live[name].to_numpy(dtype=float) if name in live else np.array([])
        lv = lv[np.isfinite(lv)]
        if name not in ref:
            inputs[name] = {
                "psi": None,
                "level": "not in training data",
                "live_rows": len(lv),
            }
            continue
        rv = ref[name].to_numpy(dtype=float)
        if len(lv) < cfg["min_live_rows"]:
            inputs[name] = {
                "psi": None,
                "level": "not enough data",
                "live_rows": len(lv),
            }
            continue
        value = psi(rv, lv)
        level = (
            "drift"
            if value > cfg["psi_drift"]
            else "moderate"
            if value > cfg["psi_moderate"]
            else "stable"
        )
        inputs[name] = {
            "psi": round(value, 3),
            "level": level,
            "live_rows": len(lv),
            "live_median": round(float(np.median(lv)), 2),
            "train_median": round(float(np.nanmedian(rv)), 2),
        }
    return {
        "checked_at_utc": now.isoformat(),
        "live_days": cfg["drift_days"],
        "reference": f"training rows of months {cfg['drift_months']} (2018-2022)",
        "reference_rows": len(ref),
        "psi_moderate": cfg["psi_moderate"],
        "psi_drift": cfg["psi_drift"],
        "inputs": inputs,
        "drift": any(v["level"] == "drift" for v in inputs.values()),
    }


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    params = load_params()
    cfg = params["monitoring"]
    now = datetime.now(UTC)
    ref = load_reference(cfg["drift_months"])
    live = load_live(now - timedelta(days=cfg["drift_days"]), params["features"])
    report = drift_report(ref, live, cfg, now)
    write_json(report, REPORT)
    for name, v in report["inputs"].items():
        log.info(
            "%-22s PSI %-6s %-15s (live rows %d)",
            name,
            v["psi"],
            v["level"],
            v["live_rows"],
        )
    log.info("Drift: %s", report["drift"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
