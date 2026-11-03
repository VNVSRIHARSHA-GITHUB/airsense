"""Alerts received from Alertmanager, kept for the Pipeline console (Stage 10).

Alertmanager posts its webhook to the backend's POST /alerts (inside the Docker
network). Each alert is kept by its fingerprint with its latest status, in
logs/alerts/alerts.json: firing alerts until they are resolved, resolved ones for
RESOLVED_DAYS days (at most KEEP_RESOLVED), so the console can show "what fired
recently" as well as "what is wrong now".
"""

import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

ALERTS_FILE = Path("logs") / "alerts" / "alerts.json"
RESOLVED_DAYS = 7
KEEP_RESOLVED = 50


def _entry(a: dict, now: str) -> dict:
    labels, notes = a.get("labels", {}), a.get("annotations", {})
    return {
        "alertname": labels.get("alertname", "unknown"),
        "severity": labels.get("severity", "warning"),
        "status": a.get("status", "firing"),
        "summary": notes.get("summary", ""),
        "description": notes.get("description", ""),
        "starts_at": a.get("startsAt"),
        # Alertmanager sends year 1 ("0001-01-01T...") for "not ended yet"
        "ends_at": a.get("endsAt") if a.get("status") == "resolved" else None,
        "updated_at": now,
    }


class AlertStore:
    """Thread-safe store of the latest status of each alert (one JSON file)."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def receive(self, payload: dict, now: datetime | None = None) -> int:
        """Merge one Alertmanager webhook payload; returns how many alerts it held."""
        now = now or datetime.now(UTC)
        alerts = payload.get("alerts", [])
        with self._lock:
            store = read_alerts_raw(self.path)
            for a in alerts:
                key = a.get("fingerprint") or json.dumps(
                    a.get("labels", {}), sort_keys=True
                )
                store[key] = _entry(a, now.isoformat())
            store = _prune(store, now)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(store, indent=2))
            tmp.replace(self.path)  # readers never see a half-written file
        return len(alerts)


def _prune(store: dict, now: datetime) -> dict:
    cutoff = (now - timedelta(days=RESOLVED_DAYS)).isoformat()
    firing = {k: v for k, v in store.items() if v["status"] == "firing"}
    resolved = sorted(
        (
            (k, v)
            for k, v in store.items()
            if v["status"] != "firing" and v["updated_at"] >= cutoff
        ),
        key=lambda kv: kv[1]["updated_at"],
        reverse=True,
    )[:KEEP_RESOLVED]
    return {**firing, **dict(resolved)}


def read_alerts_raw(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def alerts_summary(path: Path) -> dict:
    """For the console: firing alerts (critical first) and the recently resolved."""
    store = read_alerts_raw(path)
    order = {"critical": 0, "warning": 1}
    firing = sorted(
        (v for v in store.values() if v["status"] == "firing"),
        key=lambda v: (order.get(v["severity"], 2), v["starts_at"] or ""),
    )
    resolved = sorted(
        (v for v in store.values() if v["status"] != "firing"),
        key=lambda v: v["updated_at"],
        reverse=True,
    )
    return {"firing": firing, "resolved": resolved[:10]}
