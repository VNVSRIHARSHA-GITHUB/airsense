"""The forecasting service behind the API (Stage 6, D53).

One request for a city:
  1. live PM2.5 features from Feast's online store, with their timestamp;
     too old or missing -> "no forecast" with a reason (D49);
  2. weather forecast from Open-Meteo (all cities, cached), through the same
     weather_features() as training; each fetch is saved (for Stage 10's
     archive-vs-forecast check, D34);
  3. 24 input rows built exactly as in training (assemble.build_rows);
  4. the MLflow model server (champion) predicts; to_level() turns the predicted
     change into PM2.5; each value gets its CPCB AQI category;
  5. explanation: top factors from SHAP of the same champion version;
  6. health guidance from Qdrant for the worst category of the next 24 hours.
"""

import json
import logging
import os
import threading
import time
import warnings
from datetime import UTC, datetime
from pathlib import Path

import httpx
import numpy as np
import pandas as pd
import yaml

from airsense.features.build_features import weather_features
from airsense.serving.assemble import aqi_name, build_rows
from airsense.serving.explain import top_factors
from airsense.training.train import to_level

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
IST = "Asia/Kolkata"
WEATHER_COLS = [
    "temperature_2m",
    "relative_humidity_2m",
    "precipitation",
    "wind_speed_10m",
    "surface_pressure",
    "cloud_cover",
    "wind_dir_sin",
    "wind_dir_cos",
]


def load_params() -> dict:
    with open(PROJECT_ROOT / "params.yaml") as f:
        return yaml.safe_load(f)


class ModelServerClient:
    """Calls `mlflow models serve` (POST /invocations, pandas 'split' JSON)."""

    def __init__(self, url: str, timeout_s: float = 10.0):
        self.url = url.rstrip("/")
        self.timeout_s = timeout_s

    def predict(self, rows: pd.DataFrame) -> np.ndarray:
        payload = {
            "dataframe_split": {
                "columns": list(rows.columns),
                "data": json.loads(rows.to_json(orient="values")),  # NaN -> null
            }
        }
        r = httpx.post(f"{self.url}/invocations", json=payload, timeout=self.timeout_s)
        r.raise_for_status()
        return np.asarray(r.json()["predictions"], dtype=float)

    def ping(self) -> bool:
        try:
            return httpx.get(f"{self.url}/ping", timeout=2).status_code == 200
        except httpx.HTTPError:
            return False


class ForecastService:
    """Holds the long-lived clients; methods return plain dicts for the API."""

    def __init__(self, params: dict | None = None):
        os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")
        warnings.filterwarnings("ignore", category=DeprecationWarning)
        import mlflow
        from feast import FeatureStore
        from mlflow.tracking import MlflowClient

        self.params = params or load_params()
        self.cities = self.params["data"]["cities"]
        self.cfg = self.params["serving"]
        self.store = FeatureStore(repo_path=str(PROJECT_ROOT / "feature_repo"))
        self.model_server = ModelServerClient(self.cfg["model_server_url"])

        # The champion version this backend explains with (same as the model server).
        mlflow.set_tracking_uri(f"sqlite:///{PROJECT_ROOT / 'mlflow.db'}")
        self.mlflow_client = MlflowClient()
        name = self.params["registry"]["model_name"]
        mv = self.mlflow_client.get_model_version_by_alias(name, "champion")
        run = self.mlflow_client.get_run(mv.run_id)
        self.model = {
            "name": name,
            "version": mv.version,
            "run_id": mv.run_id,
            "target_mode": run.data.tags.get("target_mode", "level"),
            "val_mae": run.data.metrics.get("val_model_mae"),
            "test_mae": run.data.metrics.get("test_model_mae"),
            "test_skill": run.data.metrics.get("test_model_skill"),
            "trained_at": datetime.fromtimestamp(
                run.info.start_time / 1000, UTC
            ).isoformat(),
            "training_data_md5": run.data.tags.get("training_data_md5"),
        }
        self.booster = mlflow.lightgbm.load_model(f"models:/{name}/{mv.version}")
        self.features = self.booster.feature_name()
        self.codes = mlflow.artifacts.load_dict(
            f"runs:/{mv.run_id}/category_codes.json"
        )

        self._weather: pd.DataFrame | None = None
        self._weather_at = 0.0
        self._weather_failed_at = -1e9
        self._weather_problem: str | None = None
        self._weather_lock = threading.Lock()
        self._guidance = None  # opened on first use (loads the embedding model)

    # ---------- inputs ----------

    def live_features(self, city: str) -> tuple[dict, pd.Timestamp | None]:
        """Newest online PM2.5 features of a city and the hour they describe."""
        service = self.store.get_feature_service("serving_features")
        d = self.store.get_online_features(
            features=service, entity_rows=[{"city": city}], full_feature_names=False
        ).to_dict(include_event_timestamps=True)
        feats = {
            k: v[0] for k, v in d.items() if not k.endswith("__ts") and k != "city"
        }
        ts = d.get("pm25_24h__ts", [None])[0]
        t = pd.Timestamp(ts, unit="s", tz="UTC") if ts else None
        return feats, t

    def weather_table(self) -> tuple[pd.DataFrame | None, str | None]:
        """Weather features for all cities, cached; (None, reason) if unavailable."""
        from airsense.ingestion.openmeteo_client import fetch_weather, save_snapshot

        with self._weather_lock:
            now = time.monotonic()
            if self._weather is not None and (
                now - self._weather_at < self.cfg["weather_cache_minutes"] * 60
            ):
                return self._weather, None
            if now - self._weather_failed_at < self.cfg["weather_retry_minutes"] * 60:
                return self._weather, self._weather_problem  # do not retry every call
            try:
                raw = fetch_weather(
                    {c: (v["lat"], v["lon"]) for c, v in self.cities.items()},
                    mode="forecast",
                    past_days=self.cfg["weather_past_days"],
                    forecast_days=self.cfg["weather_forecast_days"],
                )
                save_snapshot(raw)  # kept for the forecast-vs-archive check (D34)
                self._weather = weather_features(raw, self.params["features"])
                self._weather_at = time.monotonic()
                return self._weather, None
            except Exception as exc:  # noqa: BLE001 - serve without weather, with a warning
                log.warning("Weather forecast unavailable: %s", exc)
                self._weather_failed_at = now
                self._weather_problem = f"weather forecast unavailable ({exc})"
                return self._weather, self._weather_problem

    def guidance_index(self):
        if self._guidance is None:
            from airsense.guidance.index import open_client, sentence_embedder

            g = self.params["guidance"]
            self._guidance = (
                open_client(PROJECT_ROOT / g["qdrant_path"]),
                sentence_embedder(g["embedding_model"]),
                g["collection"],
            )
        return self._guidance

    # ---------- API operations ----------

    def guidance(self, category: str, group: str = "general") -> list[dict]:
        from airsense.guidance.index import search

        client, embed, collection = self.guidance_index()
        return search(client, collection, embed, category, group)

    def forecast(self, city: str, group: str = "general") -> dict:
        if city not in self.cities:
            raise KeyError(city)
        now = pd.Timestamp.now(tz="UTC")
        feats, t = self.live_features(city)
        current = feats.get("pm25_24h")
        out = {
            "city": city,
            "region": self.cities[city]["region"],
            "generated_at_utc": now.isoformat(),
            "model": {k: self.model[k] for k in ("name", "version", "target_mode")},
            "warnings": [],
        }
        if city not in self.codes["city"]:
            return {**out, "available": False, "reason": "city not known to the model"}
        max_age = self.params["validation"]["processed"]["max_live_age_hours"]
        age_h = None if t is None else (now - t).total_seconds() / 3600
        if current is None or t is None or age_h > max_age:
            reason = (
                "no current PM2.5 value for this city"
                if current is None or t is None
                else f"latest PM2.5 value is {age_h:.1f} h old (limit {max_age} h)"
            )
            return {**out, "available": False, "reason": reason}

        rain_col = f"rain_{self.params['features']['rain_window_hours']}h"
        weather, problem = self.weather_table()
        if problem:
            out["warnings"].append(problem)
        city_weather = (
            weather[weather["city"] == city]
            if weather is not None
            else pd.DataFrame(columns=["time_utc", *WEATHER_COLS, rain_col])
        )
        rows, target_times, missing = build_rows(
            pm={f: feats[f] for f in self.features if f in feats},
            t=t,
            weather=city_weather,
            weather_cols=[*WEATHER_COLS, rain_col],
            city_code=self.codes["city"][city],
            region_code=self.codes["region"][self.cities[city]["region"]],
            cal_cfg=self.params["features"],
            feature_order=self.features,
            horizons=self.params["forecast"]["horizon_hours"],
        )
        empty = [f for f in self.features if f in feats and feats[f] is None]
        if empty:
            out["warnings"].append(f"PM2.5 history missing for: {', '.join(empty)}")
        if missing:
            out["warnings"].append(
                f"weather missing for {missing} of {len(rows) + 1} hours (now + {len(rows)} ahead)"
            )

        start = time.perf_counter()
        raw = self.model_server.predict(rows)
        pm25 = to_level(raw, np.full(len(raw), current), self.model["target_mode"])
        latency_ms = (time.perf_counter() - start) * 1000

        hours = [
            {
                "horizon": int(h),
                "time_utc": tt.isoformat(),
                "time_ist": tt.tz_convert(IST).strftime("%a %d %b %H:%M"),
                "pm25": round(float(v), 1),
                "aqi_category": aqi_name(float(v)),
            }
            for h, tt, v in zip(rows["horizon"], target_times, pm25, strict=True)
        ]
        worst = max(hours, key=lambda x: x["pm25"])
        contrib = self.booster.predict(rows, pred_contrib=True)
        guidance = []
        try:
            guidance = self.guidance(worst["aqi_category"], group)
        except Exception as exc:  # noqa: BLE001 - forecast still useful without it
            out["warnings"].append(f"guidance unavailable ({exc})")
        return {
            **out,
            "available": True,
            "current": {
                "pm25_24h": round(float(current), 1),
                "aqi_category": aqi_name(float(current)),
                "time_utc": t.isoformat(),
                "time_ist": t.tz_convert(IST).strftime("%a %d %b %H:%M"),
                "age_hours": round(age_h, 1),
                "stations": int(feats.get("n_stations_24h") or 0),
            },
            "forecast": hours,
            "worst_next_24h": {
                k: worst[k] for k in ("time_ist", "pm25", "aqi_category")
            },
            "explanation": top_factors(contrib, self.features, self.cfg["top_factors"]),
            "guidance": guidance[0] if guidance else None,
            "model_latency_ms": round(latency_ms, 1),
        }

    def cities_overview(self) -> list[dict]:
        rows = []
        now = pd.Timestamp.now(tz="UTC")
        for city, v in self.cities.items():
            feats, t = self.live_features(city)
            value = feats.get("pm25_24h")
            rows.append(
                {
                    "city": city,
                    "region": v["region"],
                    "pm25_24h": None if value is None else round(float(value), 1),
                    "aqi_category": None if value is None else aqi_name(float(value)),
                    "age_hours": None
                    if t is None
                    else round((now - t).total_seconds() / 3600, 1),
                }
            )
        return rows

    def health(self) -> dict:
        name = self.model["name"]
        try:
            registry_champion = self.mlflow_client.get_model_version_by_alias(
                name, "champion"
            ).version
        except Exception:  # noqa: BLE001 - health reports it instead of failing
            registry_champion = None
        checks = {
            "model_server": self.model_server.ping(),
            "feature_store": self.live_features(next(iter(self.cities)))[1] is not None,
            "champion_up_to_date": str(registry_champion) == str(self.model["version"]),
        }
        return {
            "status": "ok" if all(checks.values()) else "degraded",
            "checks": checks,
            "model_version": self.model["version"],
            "registry_champion": registry_champion,
        }
