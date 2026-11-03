"""Prometheus metrics of the AirSense backend (Stage 10).

GET /metrics returns them in Prometheus' text format. Two kinds:

- counted as requests happen: requests and their duration per endpoint, forecasts
  made or unavailable per city, model-server call time;
- read when Prometheus asks (at most every CACHE_SECONDS): dependencies up, age of
  the newest measurement per city, champion version, live-data collection (from
  the snapshot files), the collector's last result (logs/collector_status.json),
  and the monitoring reports: forecast vs actual and drift (data/monitoring/reports).

Each app gets its own registry, so tests can build many apps in one process.
/metrics is reachable only inside the Docker network and on 127.0.0.1 (nginx
does not forward it), so visitors cannot read it.
"""

import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)
from prometheus_client.core import GaugeMetricFamily

from airsense.serving.pipeline import SNAPSHOT_DIR, collection_health, snapshot_times

log = logging.getLogger(__name__)

CACHE_SECONDS = (
    15  # Prometheus scrapes every 30 s; health checks are not repeated more often
)
COLLECTOR_STATUS = Path("logs") / "collector_status.json"
REPORTS = Path("data") / "monitoring" / "reports"  # written by airsense_monitor
DRIFT_LEVEL = {"stable": 0, "moderate": 1, "drift": 2}
RESULTS = ("ok", "partial", "empty", "error")


class Metrics:
    """The backend's metrics; `svc` returns the ForecastService (None while starting)."""

    def __init__(self, svc, root: Path):
        self.svc, self.root = svc, root
        self.registry = CollectorRegistry()
        self.requests = Counter(
            "airsense_http_requests",
            "API requests by endpoint and status code",
            ["endpoint", "method", "status"],
            registry=self.registry,
        )
        self.duration = Histogram(
            "airsense_http_request_duration_seconds",
            "Time to answer an API request",
            ["endpoint"],
            buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10),
            registry=self.registry,
        )
        self.forecasts = Counter(
            "airsense_forecasts",
            "Forecast requests by city and result (made, unavailable, error)",
            ["city", "result"],
            registry=self.registry,
        )
        self.model_call = Histogram(
            "airsense_model_call_seconds",
            "Time of the model-server call for one forecast (24 rows)",
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2),
            registry=self.registry,
        )
        self._cache: list = []
        self._at = -1e9
        self.registry.register(self)  # collect() below runs on every scrape

    # ---------- counted as requests happen ----------

    def observe_request(self, endpoint: str, method: str, status: int, seconds: float):
        self.requests.labels(endpoint, method, str(status)).inc()
        self.duration.labels(endpoint).observe(seconds)

    def observe_forecast(self, city: str, result: dict | None, error: bool = False):
        if error:
            self.forecasts.labels(city, "error").inc()
            return
        made = bool(result and result.get("available"))
        self.forecasts.labels(city, "made" if made else "unavailable").inc()
        if made and result.get("model_latency_ms") is not None:
            self.model_call.observe(result["model_latency_ms"] / 1000)

    def render(self) -> tuple[bytes, str]:
        return generate_latest(self.registry), CONTENT_TYPE_LATEST

    # ---------- read on scrape (custom collector) ----------

    def describe(self):
        return []  # values are only known when collected

    def collect(self):
        if time.monotonic() - self._at > CACHE_SECONDS:
            self._cache = self._read()
            self._at = time.monotonic()
        yield from self._cache

    def _read(self) -> list:
        out = []
        svc = self.svc()
        if svc is not None:
            out += self._service_metrics(svc)
        out += self._collection_metrics()
        out += self._collector_metrics()
        out += self._report_metrics()
        return out

    def _service_metrics(self, svc) -> list:
        out = []
        up = GaugeMetricFamily(
            "airsense_dependency_up",
            "1 if a dependency answers its health check",
            labels=["dependency"],
        )
        try:
            for name, ok in svc.health().get("checks", {}).items():
                up.add_metric([name], 1.0 if ok else 0.0)
        except Exception as exc:  # noqa: BLE001 - a scrape must never fail
            log.warning("health check for metrics failed: %s", exc)
        out.append(up)

        age = GaugeMetricFamily(
            "airsense_data_age_hours",
            "Age of the newest PM2.5 value per city",
            labels=["city"],
        )
        has = GaugeMetricFamily(
            "airsense_city_has_data",
            "1 if the city has a recent PM2.5 value",
            labels=["city"],
        )
        try:
            for c in svc.cities_overview():
                has.add_metric([c["city"]], 0.0 if c.get("pm25_24h") is None else 1.0)
                if c.get("age_hours") is not None:
                    age.add_metric([c["city"]], float(c["age_hours"]))
        except Exception as exc:  # noqa: BLE001 - Redis down: no values this time
            log.warning("city overview for metrics failed: %s", exc)
        out += [age, has]

        version = GaugeMetricFamily(
            "airsense_champion_version", "Model version the backend serves"
        )
        try:
            version.add_metric([], float(svc.model["version"]))
        except (KeyError, TypeError, ValueError):
            pass
        out.append(version)
        return out

    def _collection_metrics(self) -> list:
        h = collection_health(
            snapshot_times(self.root / SNAPSHOT_DIR), datetime.now(UTC)
        )
        snaps = GaugeMetricFamily(
            "airsense_snapshots_last_24h", "CPCB snapshots saved in the last 24 hours"
        )
        snaps.add_metric([], float(h["snapshots_24h"]))
        since = GaugeMetricFamily(
            "airsense_minutes_since_last_snapshot", "Minutes since the newest snapshot"
        )
        if h["minutes_since_last"] is not None:
            since.add_metric([], float(h["minutes_since_last"]))
        return [snaps, since]

    def _collector_metrics(self) -> list:
        try:
            st = json.loads((self.root / COLLECTOR_STATUS).read_text())
        except (OSError, ValueError):
            return []  # collector has not written its status yet
        last = GaugeMetricFamily(
            "airsense_collector_last_result",
            "1 for the result of the collector's latest run (ok, partial, empty, error)",
            labels=["result"],
        )
        for r in RESULTS:
            last.add_metric([r], 1.0 if st.get("result") == r else 0.0)
        out = [last]
        for key, name, text in (
            (
                "time_utc",
                "airsense_collector_last_run_timestamp_seconds",
                "When the collector last ran",
            ),
            (
                "last_ok_utc",
                "airsense_collector_last_ok_timestamp_seconds",
                "When the collector last saved data",
            ),
        ):
            if st.get(key):
                g = GaugeMetricFamily(name, text)
                g.add_metric([], datetime.fromisoformat(st[key]).timestamp())
                out.append(g)
        return out

    def _report_metrics(self) -> list:
        out = []
        try:
            ev = json.loads((self.root / REPORTS / "evaluation.json").read_text())
        except (OSError, ValueError):
            ev = None
        if ev:
            o = ev["overall"]
            mae = GaugeMetricFamily(
                "airsense_forecast_mae",
                "Mean absolute error of the saved forecasts vs the measured values, "
                "last window_days days (model and the no-change forecast)",
                labels=["forecast"],
            )
            for label, key in (("model", "mae_model"), ("no_change", "mae_no_change")):
                if o.get(key) is not None:
                    mae.add_metric([label], float(o[key]))
            out.append(mae)
            for name, text, value in (
                (
                    "airsense_forecast_pairs",
                    "Forecast/actual pairs in the window",
                    o["pairs"],
                ),
                (
                    "airsense_forecast_skill",
                    "1 - model error / no-change error",
                    o["skill"],
                ),
                (
                    "airsense_forecast_error_limit",
                    "Error above which ForecastErrorHigh may fire",
                    ev.get("limit_mae"),
                ),
                (
                    "airsense_forecast_error_high",
                    "1 if the error is high and worse than no change",
                    1 if ev.get("error_high") else 0,
                ),
                (
                    "airsense_forecast_evaluated_timestamp_seconds",
                    "When the check last ran",
                    datetime.fromisoformat(ev["evaluated_at_utc"]).timestamp(),
                ),
            ):
                if value is not None:
                    g = GaugeMetricFamily(name, text)
                    g.add_metric([], float(value))
                    out.append(g)
            by_h = GaugeMetricFamily(
                "airsense_forecast_mae_by_horizon",
                "Model error by hours ahead",
                labels=["hours_ahead"],
            )
            for group, v in ev.get("by_horizon", {}).items():
                if v.get("mae_model") is not None:
                    by_h.add_metric([group], float(v["mae_model"]))
            out.append(by_h)
        try:
            dr = json.loads((self.root / REPORTS / "drift.json").read_text())
        except (OSError, ValueError):
            dr = None
        if dr:
            psi = GaugeMetricFamily(
                "airsense_drift_psi",
                "Population Stability Index, live vs training",
                labels=["input"],
            )
            level = GaugeMetricFamily(
                "airsense_drift_level",
                "0 stable, 1 moderate, 2 drift",
                labels=["input"],
            )
            for name, v in dr["inputs"].items():
                if v.get("psi") is not None:
                    psi.add_metric([name], float(v["psi"]))
                    level.add_metric([name], float(DRIFT_LEVEL.get(v["level"], 0)))
            checked = GaugeMetricFamily(
                "airsense_drift_checked_timestamp_seconds",
                "When the drift check last ran",
            )
            checked.add_metric(
                [], datetime.fromisoformat(dr["checked_at_utc"]).timestamp()
            )
            out += [psi, level, checked]
        return out
