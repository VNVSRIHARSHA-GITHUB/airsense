"""Forecast vs actual: how good were the saved forecasts? (Stage 10)

Run hourly by airsense_monitor, after record_forecasts. Joins every saved forecast
(data/monitoring/forecasts/) whose target hour has passed with the value actually
measured then (data/processed/live_city_hourly, the 24-hour average per city and
hour) and compares two forecasts of the same hour:

    model      the forecast AirSense served
    no change  the measured value the forecast started from (persistence)

The same forecast saved by several hourly runs (same city, start hour and hours
ahead) counts once: the newest saved copy is kept.

Writes data/monitoring/reports/evaluation.json: error of both over the last
`eval_window_days`, by hours ahead and by city, a daily series for the console,
and `error_high` - true when the model's error is above `error_ratio_limit` x its
test error AND worse than "no change" (needs at least `min_pairs` pairs). "Its test
error" is the served champion's (MLflow registry), not the latest training run's:
a retrain that was not promoted must not move the limit. If the registry cannot
be read, the latest training run's test error is used, and the report says so. The
backend turns the file into Prometheus metrics; the ForecastErrorHigh alert and
the retrain decision use `error_high`.
"""

import json
import logging
import os
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

from airsense.common.settings import PROJECT_ROOT, load_params

log = logging.getLogger(__name__)

FORECASTS = PROJECT_ROOT / "data" / "monitoring" / "forecasts"
ACTUALS = PROJECT_ROOT / "data" / "processed" / "live_city_hourly"
TRAIN_METRICS = PROJECT_ROOT / "data" / "interim" / "train_metrics.json"
REPORT = PROJECT_ROOT / "data" / "monitoring" / "reports" / "evaluation.json"
HORIZON_GROUPS = {"1-6 h": (1, 6), "7-12 h": (7, 12), "13-24 h": (13, 24)}


def load_forecasts(folder: Path) -> pd.DataFrame:
    files = sorted(folder.glob("*/*.parquet"))
    if not files:
        return pd.DataFrame()
    df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    # one row per distinct forecast: newest copy of (city, start hour, hours ahead)
    return (
        df.sort_values("made_at_utc")
        .drop_duplicates(["city", "base_time_utc", "horizon"], keep="last")
        .reset_index(drop=True)
    )


def load_actuals(folder: Path) -> pd.DataFrame:
    if not folder.exists():
        return pd.DataFrame(columns=["city", "target_time_utc", "actual"])
    a = pd.read_parquet(folder, columns=["city", "time_utc", "pm25_24h"])
    a["city"] = a["city"].astype(str)
    a["time_utc"] = pd.to_datetime(a["time_utc"], utc=True)
    return a.rename(columns={"time_utc": "target_time_utc", "pm25_24h": "actual"})


def match(forecasts: pd.DataFrame, actuals: pd.DataFrame) -> pd.DataFrame:
    """Forecast rows whose target hour has a measured value, with both errors."""
    if forecasts.empty or actuals.empty:
        return pd.DataFrame()
    f = forecasts.copy()
    f["target_time_utc"] = pd.to_datetime(f["target_time_utc"], utc=True)
    m = f.merge(actuals.dropna(subset=["actual"]), on=["city", "target_time_utc"])
    m["err_model"] = (m["pm25_forecast"] - m["actual"]).abs()
    m["err_no_change"] = (m["base_pm25"] - m["actual"]).abs()
    return m


def _scores(m: pd.DataFrame) -> dict:
    if m.empty:
        return {"pairs": 0, "mae_model": None, "mae_no_change": None, "skill": None}
    model, base = float(m["err_model"].mean()), float(m["err_no_change"].mean())
    return {
        "pairs": len(m),
        "mae_model": round(model, 2),
        "mae_no_change": round(base, 2),
        "skill": round(1 - model / base, 3) if base > 0 else None,
    }


def evaluate(
    pairs: pd.DataFrame, cfg: dict, test_mae: float | None, now: datetime
) -> dict:
    window = pairs
    if not pairs.empty:
        start = now - timedelta(days=cfg["eval_window_days"])
        window = pairs[pairs["target_time_utc"] >= start]
    overall = _scores(window)
    limit = round(cfg["error_ratio_limit"] * test_mae, 2) if test_mae else None
    error_high = bool(
        limit is not None
        and overall["pairs"] >= cfg["min_pairs"]
        and overall["mae_model"] > limit
        and overall["mae_model"] > overall["mae_no_change"]
    )
    by_h = {}
    for name, (lo, hi) in HORIZON_GROUPS.items():
        part = window[window["horizon"].between(lo, hi)] if not window.empty else window
        by_h[name] = _scores(part)
    by_city = (
        {c: _scores(g) for c, g in window.groupby("city")} if not window.empty else {}
    )
    daily = []
    if not pairs.empty:
        days = pairs["target_time_utc"].dt.tz_convert("Asia/Kolkata").dt.date
        for d, g in pairs.groupby(days):
            daily.append({"date": str(d), **_scores(g)})
    return {
        "evaluated_at_utc": now.isoformat(),
        "window_days": cfg["eval_window_days"],
        "test_mae": round(test_mae, 2) if test_mae else None,
        "limit_mae": limit,
        "min_pairs": cfg["min_pairs"],
        "overall": overall,
        "error_high": error_high,
        "by_horizon": by_h,
        "by_city": by_city,
        "daily": daily[-14:],
        "first_target_utc": (
            pairs["target_time_utc"].min().isoformat() if not pairs.empty else None
        ),
    }


def champion_test_mae(model_name: str) -> tuple[float, str] | None:
    """(test MAE, version) of the registry's champion, or None if unavailable."""
    try:
        import mlflow
        from mlflow import MlflowClient

        mlflow.set_tracking_uri(
            os.environ.get("MLFLOW_TRACKING_URI")
            or f"sqlite:///{PROJECT_ROOT / 'mlflow.db'}"
        )
        client = MlflowClient()
        mv = client.get_model_version_by_alias(model_name, "champion")
        mae = client.get_run(mv.run_id).data.metrics["test_model_mae"]
        return float(mae), str(mv.version)
    except Exception as exc:  # noqa: BLE001 - any registry problem: use the fallback
        log.warning("Champion's test error not readable from MLflow: %s", exc)
        return None


def reference_test_mae(
    champion: Callable[[], tuple[float, str] | None], train_metrics: Path
) -> tuple[float | None, str]:
    """The test error the limit is based on, and where it came from."""
    got = champion()
    if got:
        return got[0], f"champion version {got[1]}"
    try:
        mae = json.loads(train_metrics.read_text())["test"]["model"]["mae"]
        return float(mae), "latest training run (registry not readable)"
    except (OSError, ValueError, KeyError):
        return None, "none: the error limit is not checked"


def write_json(obj: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=2))
    tmp.replace(path)  # the backend never reads a half-written file


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    params = load_params()
    cfg = params["monitoring"]
    name = params["registry"]["model_name"]
    test_mae, source = reference_test_mae(
        lambda: champion_test_mae(name), TRAIN_METRICS
    )
    log.info("Test error for the limit: %s (%s)", test_mae, source)
    pairs = match(load_forecasts(FORECASTS), load_actuals(ACTUALS))
    report = evaluate(pairs, cfg, test_mae, datetime.now(UTC))
    report["test_mae_from"] = source
    write_json(report, REPORT)
    o = report["overall"]
    log.info(
        "Last %d days: %d pairs; model MAE %s, no-change MAE %s, skill %s; "
        "error high: %s",
        cfg["eval_window_days"],
        o["pairs"],
        o["mae_model"],
        o["mae_no_change"],
        o["skill"],
        report["error_high"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
