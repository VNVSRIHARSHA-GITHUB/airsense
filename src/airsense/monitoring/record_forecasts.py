"""Save the current forecast of every city, to check it against reality later (Stage 10).

Run every hour by Airflow (airsense_monitor). Asks the running backend for each
city's forecast, exactly what a visitor would see, and saves one row per city and
forecast hour to data/monitoring/forecasts/<UTC date>/<HHMM>.parquet:

    made_at_utc, model_version, city, base_time_utc, base_pm25,
    horizon, target_time_utc, pm25_forecast

base_pm25 is the measured 24-hour average the forecast started from, which is
also the "no change" (persistence) forecast for every hour. Once a target hour
has passed, the measured value at target_time_utc shows how good both were.
Cities without a forecast (stale data) are counted, not saved.

Exit codes: 0 saved, 1 the backend did not answer or every forecast request
failed (Airflow marks the run failed).
"""

import logging
import os
import sys
from datetime import UTC, datetime

import httpx
import pandas as pd

from airsense.common.settings import PROJECT_ROOT

log = logging.getLogger(__name__)

API = os.environ.get("AIRSENSE_API", "http://127.0.0.1:8000")  # the backend container
OUT_DIR = PROJECT_ROOT / "data" / "monitoring" / "forecasts"
TIMEOUT_S = 60


def rows_from(forecast: dict, made_at: datetime) -> list[dict]:
    """One row per forecast hour of one city's /forecast answer (none if unavailable)."""
    if not forecast.get("available"):
        return []
    cur = forecast["current"]
    return [
        {
            "made_at_utc": made_at,
            "model_version": str(forecast["model"]["version"]),
            "city": forecast["city"],
            "base_time_utc": pd.Timestamp(cur["time_utc"]),
            "base_pm25": float(cur["pm25_24h"]),
            "horizon": int(h["horizon"]),
            "target_time_utc": pd.Timestamp(h["time_utc"]),
            "pm25_forecast": float(h["pm25"]),
        }
        for h in forecast["forecast"]
    ]


def record(client: httpx.Client, out_dir=OUT_DIR, now: datetime | None = None) -> dict:
    """Ask for every city's forecast and save them in one file. Returns a summary."""
    now = now or datetime.now(UTC)
    cities = [c["city"] for c in client.get("/cities").raise_for_status().json()]
    rows, unavailable, failed = [], [], []
    for city in cities:
        try:
            r = client.get(f"/forecast/{city}")
            r.raise_for_status()
        except httpx.HTTPError as exc:
            failed.append(city)
            log.warning("No forecast for %s: %s", city, exc)
            continue
        city_rows = rows_from(r.json(), now)
        if not city_rows:
            unavailable.append(city)
        rows += city_rows
    path = None
    if rows:
        day = out_dir / f"{now:%Y-%m-%d}"
        day.mkdir(parents=True, exist_ok=True)
        path = day / f"{now:%H%M}.parquet"
        pd.DataFrame(rows).to_parquet(path, index=False)
    return {
        "cities": len(cities),
        "saved": len({r["city"] for r in rows}),
        "unavailable": unavailable,
        "failed": failed,
        "path": path,
    }


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        with httpx.Client(base_url=API, timeout=TIMEOUT_S) as client:
            s = record(client)
    except httpx.HTTPError as exc:
        log.error("Backend at %s did not answer: %s", API, exc)
        return 1
    if s["failed"] and not s["saved"]:  # e.g. model server down: nothing to check later
        log.error("No forecast could be saved; failed: %s", s["failed"])
        return 1
    log.info(
        "Saved forecasts of %d of %d cities to %s; no forecast: %s; failed: %s",
        s["saved"],
        s["cities"],
        s["path"],
        s["unavailable"] or "none",
        s["failed"] or "none",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
