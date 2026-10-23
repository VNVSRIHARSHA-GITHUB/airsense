"""Download historical hourly weather (Open-Meteo archive) for training (Stage 3.2).

Fetches the archive (reanalysis) weather for every configured city, one calendar
year per request (all cities together), and saves one Parquet file per year:

  data/raw/weather/archive/<year>.parquet   one row per (city, UTC hour)
  data/interim/fetch_weather_metrics.json   rows, missing values, timing

Times are stored as naive UTC, like every other table in the project.
Open-Meteo counts a long multi-city request as many API calls (free tier: 600 per
minute), so the script pauses between years (params.yaml -> weather.archive.pause_s).
"""

import json
import logging
import sys
import time
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd
import yaml

from airsense.ingestion.openmeteo_client import ARCHIVE_URL, HOURLY_VARS, fetch_weather

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
OUT_DIR = PROJECT_ROOT / "data" / "raw" / "weather" / "archive"
METRICS_FILE = PROJECT_ROOT / "data" / "interim" / "fetch_weather_metrics.json"


def year_ranges(start: str, end: str) -> list[tuple[str, str]]:
    """Split [start, end] (YYYY-MM-DD, inclusive) into calendar-year chunks."""
    s, e = date.fromisoformat(start), date.fromisoformat(end)
    if s > e:
        raise ValueError(f"start_date {start} is after end_date {end}")
    return [
        (max(s, date(y, 1, 1)).isoformat(), min(e, date(y, 12, 31)).isoformat())
        for y in range(s.year, e.year + 1)
    ]


def expected_rows(start: str, end: str, n_cities: int) -> int:
    """Hours from 00:00 on start to 23:00 on end, for every city."""
    days = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
    return days * 24 * n_cities


def to_naive_utc(df: pd.DataFrame) -> pd.DataFrame:
    """Drop the UTC time-zone label (the values stay the same) to match other tables."""
    return df.assign(time_utc=df["time_utc"].dt.tz_convert("UTC").dt.tz_localize(None))


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    with open(PROJECT_ROOT / "params.yaml") as f:
        params = yaml.safe_load(f)
    cities = params["data"]["cities"]
    cfg = params["weather"]["archive"]
    locations = {c: (v["lat"], v["lon"]) for c, v in cities.items()}
    chunks = year_ranges(cfg["start_date"], cfg["end_date"])

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    per_year = {}
    download_s = 0.0
    for i, (a, b) in enumerate(chunks):
        if i:
            time.sleep(cfg["pause_s"])  # stay under the per-minute API limit
        t0 = time.perf_counter()
        try:
            df = fetch_weather(locations, mode="archive", start_date=a, end_date=b)
        except RuntimeError as exc:
            log.error("Archive download failed for %s .. %s: %s", a, b, exc)
            return 1
        seconds = time.perf_counter() - t0
        download_s += seconds

        df = to_naive_utc(df)
        year = a[:4]
        df.to_parquet(OUT_DIR / f"{year}.parquet", index=False)
        per_year[year] = {
            "start_date": a,
            "end_date": b,
            "rows": len(df),
            "expected_rows": expected_rows(a, b, len(cities)),
            "missing_values": {v: int(df[v].isna().sum()) for v in HOURLY_VARS},
            "seconds": round(seconds, 1),
        }
        log.info(
            "%s: %d rows (%d expected) in %.1f s",
            year,
            len(df),
            per_year[year]["expected_rows"],
            seconds,
        )

    total = sum(y["rows"] for y in per_year.values())
    metrics = {
        "fetched_at_utc": datetime.now(UTC).isoformat(),
        "source": ARCHIVE_URL,
        "variables": HOURLY_VARS,
        "cities": len(cities),
        "start_date": cfg["start_date"],
        "end_date": cfg["end_date"],
        "rows": total,
        "download_seconds": round(download_s, 1),  # excludes the pauses
        "rows_per_second": round(total / download_s) if download_s else None,
        "years": per_year,
    }
    METRICS_FILE.parent.mkdir(parents=True, exist_ok=True)
    METRICS_FILE.write_text(json.dumps(metrics, indent=2))

    short = [y for y, m in per_year.items() if m["rows"] != m["expected_rows"]]
    if short:
        # Completeness is checked properly in validate_weather (Stage 3.3).
        log.warning("Row count differs from expected for: %s", short)
    log.info(
        "Saved %d rows (%d years) in %.1f s of downloading",
        total,
        len(per_year),
        download_s,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
