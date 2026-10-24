"""Client for Open-Meteo weather data (free, no API key).

Two endpoints:
- forecast: recent past days + the next days, used for live serving.
- archive:  historical reanalysis (ERA5) back to 1940, used for training.

Both return the same tidy table: one row per (city, UTC hour) with one
column per weather variable.
"""

import logging
import time
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import requests

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
OUT_DIR = PROJECT_ROOT / "data" / "raw" / "live" / "openmeteo"

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
HOURLY_VARS = [
    "temperature_2m",  # deg C at 2 m
    "relative_humidity_2m",  # %
    "precipitation",  # mm in the hour (rain washes PM2.5 out)
    "wind_speed_10m",  # km/h (wind disperses pollution)
    "wind_direction_10m",  # degrees (where pollution blows from)
    "surface_pressure",  # hPa (high pressure -> stagnant air)
    "cloud_cover",  # %
]
HEADERS = {"User-Agent": "AirSense-collector/1.0 (DA3408 course project)"}
TIMEOUT_S = 60
MAX_RETRIES = 3


def _get(
    url: str, params: dict, timeout_s: float = TIMEOUT_S, retries: int = MAX_RETRIES
) -> dict | list:
    """GET with retries; returns the parsed JSON body.

    The defaults suit the training download (patient); the API passes a short
    timeout and a single attempt so a user never waits minutes for weather.
    """
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, params=params, headers=HEADERS, timeout=timeout_s)
            if resp.status_code != 200:
                # Open-Meteo explains bad requests in the body ("reason").
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            return resp.json()
        except (requests.RequestException, RuntimeError, ValueError) as exc:
            log.warning("Open-Meteo attempt %d/%d failed: %s", attempt, retries, exc)
            if attempt == retries:
                raise RuntimeError("Open-Meteo request failed") from None
            time.sleep(10 * attempt)


def _to_frame(item: dict, city: str) -> pd.DataFrame:
    """Turn one location's JSON block into a table with a city column."""
    df = pd.DataFrame(item["hourly"]).rename(columns={"time": "time_utc"})
    df["time_utc"] = pd.to_datetime(df["time_utc"], utc=True)
    df.insert(0, "city", city)
    return df


def fetch_weather(
    locations: dict[str, tuple[float, float]],
    mode: str = "forecast",
    start_date: str | None = None,
    end_date: str | None = None,
    past_days: int = 2,
    forecast_days: int = 2,
    timeout_s: float = TIMEOUT_S,
    retries: int = MAX_RETRIES,
) -> pd.DataFrame:
    """Fetch hourly weather for several cities in one request.

    locations: {"Chennai": (13.08, 80.27), ...}
    mode: "forecast" (live) or "archive" (history; needs start/end dates
          as "YYYY-MM-DD").
    """
    names = list(locations)
    params = {
        "latitude": ",".join(str(locations[c][0]) for c in names),
        "longitude": ",".join(str(locations[c][1]) for c in names),
        "hourly": ",".join(HOURLY_VARS),
        "timezone": "GMT",
    }
    if mode == "forecast":
        url = FORECAST_URL
        params |= {"past_days": past_days, "forecast_days": forecast_days}
    elif mode == "archive":
        if not (start_date and end_date):
            raise ValueError("archive mode needs start_date and end_date")
        url = ARCHIVE_URL
        params |= {"start_date": start_date, "end_date": end_date}
    else:
        raise ValueError(f"unknown mode: {mode}")

    body = _get(url, params, timeout_s=timeout_s, retries=retries)
    # One location -> a JSON object; several -> a list in the same order.
    items = body if isinstance(body, list) else [body]
    return pd.concat(
        [_to_frame(item, city) for item, city in zip(items, names)], ignore_index=True
    )


def save_snapshot(df: pd.DataFrame, out_dir: Path = OUT_DIR) -> Path:
    """Save a forecast snapshot as CSV, in a folder per day (UTC)."""
    fetched_at = datetime.now(UTC)
    df = df.assign(fetched_at_utc=fetched_at.isoformat())
    day_dir = out_dir / fetched_at.strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / f"{fetched_at:%H%M}.csv"
    df.to_csv(path, index=False)
    return path
