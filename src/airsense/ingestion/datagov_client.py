"""Client for the data.gov.in real-time CPCB air-quality feed.

Downloads a snapshot of the live feed for one pollutant (PM2.5) across all
reporting stations and saves it as a timestamped CSV under
data/raw/live/datagov/<UTC date>/<HHMM>.csv.

Exit codes: 0 saved (also a partial snapshot), 99 the feed answered but had no
records (a source-side gap, not our fault: Airflow marks the run "skipped"),
1 anything else (no key, network or API errors after retries).
Each run writes its result to logs/collector_status.json for the backend's metrics.
"""
import json
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
STATUS_FILE = PROJECT_ROOT / "logs" / "collector_status.json"
EXIT_SOURCE_EMPTY = 99  # Airflow's collect task treats this code as "skipped"

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


def write_status(result: str, records: int, path: Path | None = None) -> None:
    """Record this run's result (ok, partial, empty, error); keeps the last good time."""
    path = path or STATUS_FILE
    now = datetime.now(UTC).isoformat()
    try:
        last_ok = json.loads(path.read_text()).get("last_ok_utc")
    except (OSError, ValueError):
        last_ok = None
    status = {"time_utc": now, "result": result, "records": records,
              "last_ok_utc": now if result in ("ok", "partial") else last_ok}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(status, indent=2))
        tmp.replace(path)  # the backend never reads a half-written file
    except OSError as exc:  # the status is for monitoring only; never fail the run
        log.warning("Could not write %s: %s", path, exc)


def main() -> int:
    try:
        return collect()
    except Exception:
        write_status("error", 0)
        raise


def collect() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_dotenv(PROJECT_ROOT / ".env")
    api_key = os.environ.get("DATA_GOV_IN_API_KEY", "")
    if not api_key:
        log.error("DATA_GOV_IN_API_KEY is not set; check the .env file")
        write_status("error", 0)
        return 1

    with open(PARAMS_FILE) as f:
        params = yaml.safe_load(f)
    resource_id = params["sources"]["datagov"]["resource_id"]

    min_records = params["sources"]["datagov"].get("min_records", 200)
    records = fetch_all_records(resource_id, api_key)
    if not records:
        # The API answered normally but the feed is empty (seen on 1 Nov 2026,
        # 18:35-19:10): a gap at the source. Not a pipeline fault, so not "failed".
        log.warning("Source empty: the feed returned 0 records; nothing saved")
        write_status("empty", 0)
        return EXIT_SOURCE_EMPTY

    path = save_snapshot(records)
    if len(records) < min_records:
        # Source-side delay (e.g. the midnight reading publishes late), not a
        # pipeline failure: keep the data, warn, and let the next run catch up.
        log.warning("Partial snapshot: %d records (expected >= %d) saved to %s",
                    len(records), min_records, path)
        write_status("partial", len(records))
    else:
        log.info("Saved %d records to %s", len(records), path)
        write_status("ok", len(records))
    return 0


if __name__ == "__main__":
    sys.exit(main())
