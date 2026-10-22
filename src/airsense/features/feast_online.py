"""Load live features into the Feast online store and check them (Stage 3.5).

1. materialize: copy the newest live PM2.5 features per city (pm_live view,
   last `features.materialize_days` days) into the SQLite online store;
2. read them back for every configured city, as the serving API will, and
   measure the read latency;
3. check freshness: Feast does not apply ttl to online reads, so the age of each
   city's newest live hour is computed here and compared with
   validation.processed.max_live_age_hours.

A missing or stale city is a WARNING (the live feed can lag); an online store with
no city at all is an error (exit code 1).

Output: data/interim/feast_online_metrics.json
"""

import json
import logging
import statistics
import sys
import time
import warnings
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import yaml
from feast import FeatureStore

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
REPO = PROJECT_ROOT / "feature_repo"
PM_LIVE = PROJECT_ROOT / "data" / "features" / "pm_live"
METRICS_FILE = PROJECT_ROOT / "data" / "interim" / "feast_online_metrics.json"


def newest_hours(pm_live: pd.DataFrame, now: pd.Timestamp) -> pd.Series:
    """Per city: the newest live hour with a value, at or before now."""
    rows = pm_live[pm_live["pm25_24h"].notna() & (pm_live["time_utc"] <= now)]
    return rows.groupby("city", observed=True)["time_utc"].max()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logging.getLogger("feast").setLevel(logging.WARNING)  # hide registry chatter
    warnings.filterwarnings("ignore", category=DeprecationWarning)
    with open(PROJECT_ROOT / "params.yaml") as f:
        params = yaml.safe_load(f)
    cities = sorted(params["data"]["cities"])
    days = params["features"]["materialize_days"]
    max_age = params["validation"]["processed"]["max_live_age_hours"]

    store = FeatureStore(repo_path=str(REPO))
    end = datetime.now(UTC)
    t0 = time.perf_counter()
    store.materialize(
        start_date=end - timedelta(days=days), end_date=end, feature_views=["pm_live"]
    )
    materialize_s = time.perf_counter() - t0

    service = store.get_feature_service("serving_features")
    rows = [{"city": c} for c in cities]
    online = store.get_online_features(
        features=service, entity_rows=rows, full_feature_names=False
    ).to_df()
    latencies = []
    for _ in range(20):  # one city per call, as the API will ask
        t = time.perf_counter()
        store.get_online_features(
            features=service, entity_rows=[{"city": "Delhi"}], full_feature_names=False
        ).to_dict()
        latencies.append((time.perf_counter() - t) * 1000)

    present = sorted(online.loc[online["pm25_24h"].notna(), "city"])
    now = pd.Timestamp(end)
    newest = newest_hours(pd.read_parquet(PM_LIVE), now)
    age_h = ((now - newest).dt.total_seconds() / 3600).round(1)
    stale = {c: float(a) for c, a in age_h.items() if a > max_age}
    missing = sorted(set(cities) - set(present))

    METRICS_FILE.parent.mkdir(parents=True, exist_ok=True)
    METRICS_FILE.write_text(
        json.dumps(
            {
                "checked_at_utc": end.isoformat(),
                "materialize_days": days,
                "materialize_seconds": round(materialize_s, 2),
                "online_read_ms_median": round(statistics.median(latencies), 2),
                "online_read_ms_max": round(max(latencies), 2),
                "cities_in_online_store": len(present),
                "missing_cities": missing,
                "max_live_age_hours": max_age,
                "live_age_hours": {c: float(a) for c, a in age_h.items()},
                "stale_cities": stale,
            },
            indent=2,
        )
    )
    if missing:
        log.warning("No online features for: %s", missing)
    if stale:
        log.warning("Online features older than %s h: %s", max_age, stale)
    log.info(
        "Online store: %d/%d cities, materialized in %.2f s, read %.2f ms (median)",
        len(present),
        len(cities),
        materialize_s,
        statistics.median(latencies),
    )
    if not present:
        log.error("The online store holds no features at all")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
