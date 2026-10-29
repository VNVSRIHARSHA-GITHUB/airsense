"""The forecasting service behind the API (Stage 6).

One request for a city:
  1. live PM2.5 features from Feast's online store, with their timestamp;
     too old or missing -> "no forecast" with a reason;
  2. weather forecast from Open-Meteo (all cities, cached), through the same
     weather_features() as training; each fetch is saved (for Stage 10's
     archive-vs-forecast check);
  3. 24 input rows built exactly as in training (assemble.build_rows);
  4. the MLflow model server (champion) predicts; to_level() turns the predicted
     change into PM2.5; each value gets its CPCB AQI category;
  5. explanation: top factors from SHAP of the same champion version;
  6. health guidance from Qdrant for the worst category of the next 24 hours.
"""

import json
import logging
import os
import tempfile
import threading
import time
import warnings
from datetime import UTC, datetime

import httpx
import numpy as np
import pandas as pd

from airsense.common.settings import PROJECT_ROOT, feature_store, load_params
from airsense.features.build_features import weather_features
from airsense.serving.assemble import aqi_name, build_rows
from airsense.serving.explain import top_factors
from airsense.training.train import to_level

log = logging.getLogger(__name__)

SNAPSHOT_DIR = PROJECT_ROOT / "data" / "raw" / "live" / "openmeteo"
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


class ModelServerError(RuntimeError):
    """The model server could not be reached or gave no usable answer."""


class FeatureStoreError(RuntimeError):
    """The online store could not be read."""


class FeatureStoreBusy(FeatureStoreError):
    """The online store answered but the read failed, e.g. during a refresh."""


class FeatureStoreDown(FeatureStoreError):
    """The online store (Redis) could not be reached at all."""


def _unreachable(exc: Exception) -> bool:
    """True for connection failures (Redis stopped, name unknown, timeout)."""
    from redis.exceptions import ConnectionError as RedisConnectionError
    from redis.exceptions import TimeoutError as RedisTimeoutError

    return isinstance(exc, RedisConnectionError | RedisTimeoutError | OSError)


def integer_columns(model_uri: str) -> set[str]:
    """Names of the inputs the registered signature declares as integers."""
    import mlflow

    try:
        inputs = mlflow.models.get_model_info(model_uri).signature.inputs.to_dict()
    except Exception as exc:  # noqa: BLE001 - without it the server still validates
        log.warning("Could not read the model signature: %s", exc)
        return set()
    return {c["name"] for c in inputs if c.get("type") in ("integer", "long")}


def _value(v) -> float | None:
    """A feature value as float, or None if missing (None or NaN)."""
    return None if v is None or not np.isfinite(float(v)) else float(v)


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
        try:
            r = httpx.post(
                f"{self.url}/invocations", json=payload, timeout=self.timeout_s
            )
        except httpx.HTTPError as exc:
            raise ModelServerError(f"model server at {self.url}: {exc}") from exc
        if r.status_code != 200:  # keep MLflow's explanation, e.g. a schema mismatch
            raise ModelServerError(f"model server HTTP {r.status_code}: {r.text[:400]}")
        try:
            return np.asarray(r.json()["predictions"], dtype=float)
        except (KeyError, ValueError) as exc:
            raise ModelServerError(f"model server gave no predictions: {exc}") from exc

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
        from mlflow.tracking import MlflowClient

        self.params = params or load_params()
        self.cities = self.params["data"]["cities"]
        self.cfg = self.params["serving"]
        self.store = feature_store()
        self.model_server = ModelServerClient(self.cfg["model_server_url"])

        # The champion version this backend explains with (same as the model server).
        mlflow.set_tracking_uri(
            os.environ.get("MLFLOW_TRACKING_URI")
            or f"sqlite:///{PROJECT_ROOT / 'mlflow.db'}"
        )
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
        # Load from a temporary copy: MLflow writes a small file into the folder
        # it loads from, and in the container mlruns/ is mounted read-only.
        self.booster = mlflow.lightgbm.load_model(
            f"models:/{name}/{mv.version}", dst_path=tempfile.mkdtemp()
        )
        self.features = self.booster.feature_name()
        self.codes = mlflow.artifacts.load_dict(
            f"runs:/{mv.run_id}/category_codes.json"
        )
        # Columns the model's input schema stores as integers: they cannot be empty,
        # otherwise the model server rejects the request (400).
        self.int_cols = integer_columns(f"models:/{name}/{mv.version}")

        self._weather: pd.DataFrame | None = None
        self._weather_at = 0.0
        self._weather_failed_at = -1e9
        self._weather_lock = threading.Lock()
        self._weather_note: str | None = None
        # Feast's SQLite online store shares one connection between threads; two
        # reads at the same moment broke it (IndexError), so reads take turns.
        self._store_lock = threading.Lock()
        self._guidance = None  # opened on first use (loads the embedding model)
        self._guidance_lock = threading.Lock()  # local Qdrant: open only once

    # ---------- inputs ----------

    def _read_online(self, city: str) -> dict:
        with self._store_lock:
            service = self.store.get_feature_service("serving_features")
            return self.store.get_online_features(
                features=service, entity_rows=[{"city": city}], full_feature_names=False
            ).to_dict(include_event_timestamps=True)

    def live_features(self, city: str) -> tuple[dict, pd.Timestamp | None]:
        """Newest online PM2.5 features of a city and the hour they describe.

        `dvc repro feast_online` rewrites the online store from another process;
        a read at that moment can fail, so it is retried once after a short pause.
        """
        try:
            d = self._read_online(city)
        except Exception as first:  # noqa: BLE001 - retried once, then reported
            log.warning("Online read for %s failed, retrying: %s", city, first)
            time.sleep(0.5)
            try:
                d = self._read_online(city)
            except Exception as exc:
                error = FeatureStoreDown if _unreachable(exc) else FeatureStoreBusy
                raise error(f"online store read failed: {exc}") from exc
        feats = {
            k: v[0] for k, v in d.items() if not k.endswith("__ts") and k != "city"
        }
        ts = d.get("pm25_24h__ts", [None])[0]
        t = pd.Timestamp(ts, unit="s", tz="UTC") if ts else None
        return feats, t

    def weather_table(self) -> tuple[pd.DataFrame | None, str | None]:
        """Weather features for all cities and a note when they are not live.

        Order: the cached live forecast (30 min); a new Open-Meteo call (short
        timeout, one attempt); after a failure, the newest saved forecast
        snapshot (each successful call is saved, and one fetch covers 3 days);
        otherwise (None, reason). Failures are retried after 5 minutes.
        """
        from airsense.ingestion.openmeteo_client import fetch_weather, save_snapshot

        with self._weather_lock:
            now = time.monotonic()
            fresh = now - self._weather_at < self.cfg["weather_cache_minutes"] * 60
            if self._weather is not None and fresh:
                return self._weather, self._weather_note
            if now - self._weather_failed_at < self.cfg["weather_retry_minutes"] * 60:
                return self._weather, self._weather_note  # do not retry every call
            try:
                raw = fetch_weather(
                    {c: (v["lat"], v["lon"]) for c, v in self.cities.items()},
                    mode="forecast",
                    past_days=self.cfg["weather_past_days"],
                    forecast_days=self.cfg["weather_forecast_days"],
                    timeout_s=self.cfg["weather_timeout_seconds"],
                    retries=1,
                )
                save_snapshot(raw, SNAPSHOT_DIR)  # input of the Stage 10 weather check
                self._weather = weather_features(raw, self.params["features"])
                self._weather_at, self._weather_note = time.monotonic(), None
            except Exception as exc:  # noqa: BLE001 - fall back to a saved forecast
                log.warning("Live weather forecast unavailable: %s", exc)
                self._weather_failed_at = now
                self._weather, self._weather_note = self.saved_weather()
            return self._weather, self._weather_note

    def saved_weather(self) -> tuple[pd.DataFrame | None, str]:
        """The newest saved Open-Meteo forecast, as weather features."""
        files = sorted(SNAPSHOT_DIR.glob("*/*.csv"))
        if not files:
            return None, "weather forecast unavailable (no saved forecast either)"
        raw = pd.read_csv(files[-1])
        raw["time_utc"] = pd.to_datetime(raw["time_utc"], utc=True)
        fetched = pd.Timestamp(raw.pop("fetched_at_utc").iloc[0]).tz_convert(IST)
        note = (
            "live weather forecast unavailable; using the forecast fetched "
            f"{fetched:%a %d %b %H:%M} IST"
        )
        return weather_features(raw, self.params["features"]), note

    def guidance_index(self):
        with self._guidance_lock:  # two first requests must not both open Qdrant
            return self._open_guidance()

    def _open_guidance(self):
        if self._guidance is None:
            from airsense.guidance.index import open_client, sentence_embedder

            g = self.params["guidance"]
            self._guidance = (
                open_client(g["qdrant_url"]),
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
        current = _value(feats.get("pm25_24h"))
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
        weather, note = self.weather_table()
        if note:
            out["warnings"].append(note)
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
            # The model was trained with complete weather, and its input schema
            # stores humidity and cloud cover as integers, which cannot be empty.
            return {
                **out,
                "available": False,
                "reason": f"weather forecast missing for {missing} of "
                f"{len(rows) + 1} hours needed",
            }

        gaps = [c for c in self.int_cols if c in rows and rows[c].isna().any()]
        if gaps:
            return {
                **out,
                "available": False,
                "reason": "input values missing that the model cannot do without: "
                + ", ".join(sorted(gaps)),
            }

        start = time.perf_counter()
        raw = self.model_server.predict(rows)  # ModelServerError -> HTTP 503
        latency_ms = (time.perf_counter() - start) * 1000
        pm25 = to_level(raw, np.full(len(raw), current), self.model["target_mode"])
        if len(pm25) != len(rows) or not np.isfinite(pm25).all():
            return {**out, "available": False, "reason": "model gave no valid forecast"}
        if (pm25 < 0).any():  # a predicted fall larger than the current value
            out["warnings"].append("negative forecasts set to 0")
            pm25 = np.clip(pm25, 0, None)

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
            log.warning("Guidance unavailable for %s: %s", city, exc)
            out["warnings"].append("health advice service unavailable right now")
        return {
            **out,
            "available": True,
            "current": {
                "pm25_24h": round(current, 1),
                "aqi_category": aqi_name(current),
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
            value = _value(feats.get("pm25_24h"))
            rows.append(
                {
                    "city": city,
                    "region": v["region"],
                    "pm25_24h": None if value is None else round(value, 1),
                    "aqi_category": aqi_name(value),
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
        try:
            store_ok = self.live_features(next(iter(self.cities)))[1] is not None
        except Exception:  # noqa: BLE001 - health reports it instead of failing
            store_ok = False
        checks = {
            "model_server": self.model_server.ping(),
            "feature_store": store_ok,
            "champion_up_to_date": str(registry_champion) == str(self.model["version"]),
        }
        return {
            "status": "ok" if all(checks.values()) else "degraded",
            "checks": checks,
            "model_version": self.model["version"],
            "registry_champion": registry_champion,
        }
