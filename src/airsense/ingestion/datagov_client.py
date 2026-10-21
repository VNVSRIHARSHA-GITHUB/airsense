"""Client for the data.gov.in real-time CPCB air-quality feed.

Downloads a snapshot of the live feed for one pollutant (PM2.5) across all
reporting stations and saves it as a timestamped CSV under
data/raw/live/datagov/<UTC date>/<HHMM>.csv.
"""
import logging
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import requests
import yaml
from dotenv import load_dotenv

log = logging.getLogger(__name__)

# Project root = three levels above this file (src/airsense/ingestion/ -> project).
PROJECT_ROOT = Path(__file__).resolve().parents[3]
PARAMS_FILE = PROJECT_ROOT / "params.yaml"
OUT_DIR = PROJECT_ROOT / "data" / "raw" / "live" / "datagov"

API_URL = "https://api.data.gov.in/resource/{rid}"
PAGE_SIZE = 200           # records per call; large pages time out on data.gov.in
TIMEOUT_S = 90            # seconds to wait for the server's response
MAX_RETRIES = 3           # attempts per page before giving up
POLLUTANT = "PM2.5"       # only fetch what the model uses
# data.gov.in throttles the default "python-requests" User-Agent (502 after ~60 s),
# so identify the client explicitly.
HEADERS = {"User-Agent": "AirSense-collector/1.0 (DA3408 course project)"}


def _redact(text: str, secret: str) -> str:
    """Remove the API key from any text before it is logged."""
    return text.replace(secret, "***") if secret else text


def fetch_page(resource_id: str, api_key: str, offset: int) -> dict:
    """Fetch one page of records, retrying on temporary failures."""
    params = {"api-key": api_key, "format": "json",
              "limit": PAGE_SIZE, "offset": offset,
              "filters[pollutant_id]": POLLUTANT}
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(API_URL.format(rid=resource_id),
                                params=params, headers=HEADERS,
                                timeout=TIMEOUT_S)
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code}")
            body = resp.json()
            if body.get("status") == "error":
                raise RuntimeError(f"API error: {body.get('message')}")
            return body
        except (requests.RequestException, RuntimeError, ValueError) as exc:
            log.warning("Page offset=%d attempt %d/%d failed: %s",
                        offset, attempt, MAX_RETRIES,
                        _redact(str(exc), api_key))
            if attempt == MAX_RETRIES:
                raise RuntimeError(f"Giving up on offset {offset}") from None
            time.sleep(30 * attempt)  # 30 s, 60 s: rides out short server outages


def fetch_all_records(resource_id: str, api_key: str) -> list[dict]:
    """Download every record in the feed, page by page."""
    records, offset = [], 0
    while True:
        body = fetch_page(resource_id, api_key, offset)
        page = body.get("records", [])
        records.extend(page)
        offset += len(page)
        if not page or offset >= int(body.get("total", 0)):
            break
    return records


def save_snapshot(records: list[dict], out_dir: Path = OUT_DIR) -> Path:
    """Save one snapshot as CSV, in a folder per day (UTC)."""
    fetched_at = datetime.now(UTC)
    df = pd.DataFrame(records)
    df["fetched_at_utc"] = fetched_at.isoformat()
    day_dir = out_dir / fetched_at.strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / f"{fetched_at:%H%M}.csv"
    df.to_csv(path, index=False)
    return path


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_dotenv(PROJECT_ROOT / ".env")
    api_key = os.environ.get("DATA_GOV_IN_API_KEY", "")
    if not api_key:
        log.error("DATA_GOV_IN_API_KEY is not set; check the .env file")
        return 1

    with open(PARAMS_FILE) as f:
        params = yaml.safe_load(f)
    resource_id = params["sources"]["datagov"]["resource_id"]

    min_records = params["sources"]["datagov"].get("min_records", 200)
    records = fetch_all_records(resource_id, api_key)
    if not records:
        log.error("Feed returned 0 records; nothing saved")
        return 1

    path = save_snapshot(records)
    if len(records) < min_records:
        # Source-side delay (e.g. the midnight reading publishes late), not a
        # pipeline failure: keep the data, warn, and let the next run catch up.
        log.warning("Partial snapshot: %d records (expected >= %d) saved to %s",
                    len(records), min_records, path)
    else:
        log.info("Saved %d records to %s", len(records), path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
