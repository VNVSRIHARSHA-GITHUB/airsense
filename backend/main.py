"""AirSense backend API (Stage 6).

Run from the project root:
    uvicorn main:app --app-dir backend --port 8000
Needs the MLflow model server running (model_server/serve.sh) for /forecast.

Endpoints (the API contract, documented in the LLD):
    GET /health              status of model server, feature store, champion version
    GET /cities              the 14 cities with the latest PM2.5 and its age
    GET /forecast/{city}     24 hourly forecasts + AQI, explanation, health guidance
    GET /model               the champion model and its metrics
    GET /guidance            advice for an AQI category and a user group
    GET /pipeline            pipeline status for the operators' console (read-only)
    GET /metrics             Prometheus metrics (Stage 10; not forwarded by nginx)
    POST /alerts             Alertmanager's webhook (Stage 10; not forwarded by nginx)
Interactive documentation: http://127.0.0.1:8000/docs
"""

import logging
import time
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from airsense.common.settings import PROJECT_ROOT
from airsense.monitoring.alerts import ALERTS_FILE, AlertStore
from airsense.serving.metrics import Metrics
from airsense.serving.service import (
    FeatureStoreBusy,
    FeatureStoreDown,
    ModelServerError,
)

log = logging.getLogger("airsense.api")


Category = Literal["Good", "Satisfactory", "Moderate", "Poor", "Very Poor", "Severe"]
Group = Literal["general", "sensitive", "outdoor_workers"]


class Hour(BaseModel):
    horizon: int
    time_utc: str
    time_ist: str
    pm25: float
    aqi_category: str | None


class Current(BaseModel):
    pm25_24h: float
    aqi_category: str | None
    time_utc: str
    time_ist: str
    age_hours: float
    stations: int


class Factor(BaseModel):
    factor: str
    effect_ug_m3: float
    direction: Literal["raises", "lowers"]


class Guidance(BaseModel):
    category: str
    pm25_range: str
    group: str
    text: str
    sources: list[str]
    score: float


class ModelRef(BaseModel):
    name: str
    version: str | int
    target_mode: str


class Forecast(BaseModel):
    city: str
    region: str
    generated_at_utc: str
    available: bool
    reason: str | None = None
    model: ModelRef
    current: Current | None = None
    forecast: list[Hour] = []
    worst_next_24h: dict | None = None
    explanation: list[Factor] = []
    explanation_other_ug_m3: float | None = None  # all factors not listed, combined
    guidance: Guidance | None = None
    warnings: list[str] = []
    model_latency_ms: float | None = None


class CityStatus(BaseModel):
    city: str
    region: str
    pm25_24h: float | None
    aqi_category: str | None
    age_hours: float | None


def create_app(service=None, pipeline=None, root=PROJECT_ROOT) -> FastAPI:
    """Build the app; tests pass fakes, normal runs create the real services."""
    state = {"service": service, "pipeline": pipeline}
    metrics = Metrics(lambda: state["service"], root)
    alert_store = AlertStore(root / ALERTS_FILE)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if state["service"] is None:
            from airsense.serving.service import ForecastService

            state["service"] = ForecastService()
            try:  # load the embedding model now, not on the first visitor's request
                state["service"].guidance_index()
            except Exception as exc:  # noqa: BLE001 - forecasts work without guidance
                log.warning("Guidance index not loaded at startup: %s", exc)
        if state["pipeline"] is None:
            from airsense.serving.pipeline import PipelineStatus

            params = state["service"].params
            cfg = params["serving"]
            state["pipeline"] = PipelineStatus(
                cache_seconds=cfg["pipeline_cache_seconds"],
                dvc_timeout_s=cfg["dvc_status_timeout_seconds"],
                airflow_url=cfg.get("airflow_url"),
                model_info=state["service"].model,
                monitoring_cfg=params.get("monitoring", {}),
                links={
                    k: cfg.get(f"{k}_url")
                    for k in ("grafana", "prometheus", "alertmanager")
                },
            )
        yield

    app = FastAPI(
        title="AirSense API",
        version="1.0",
        description="24-hour PM2.5 forecasts and health guidance for 14 Indian cities. "
        "Data: CPCB (via Kaggle and data.gov.in); weather: Open-Meteo.com.",
        lifespan=lifespan,
    )
    app.add_middleware(  # the separate frontend (Stage 7) calls this API
        CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"]
    )

    @app.middleware("http")
    async def count_requests(request: Request, call_next):
        start = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            route = request.scope.get("route")  # the template, e.g. /forecast/{city}
            endpoint = getattr(route, "path", "other")
            if endpoint != "/metrics":
                metrics.observe_request(
                    endpoint, request.method, status, time.perf_counter() - start
                )

    def svc():
        return state["service"]

    def unavailable(what: str, exc: Exception) -> HTTPException:
        log.error("%s unavailable: %s", what, exc)
        return HTTPException(503, f"{what} unavailable")

    @app.get("/health")
    def health() -> dict:
        return svc().health()

    @app.get("/cities", response_model=list[CityStatus])
    def cities():
        try:
            return svc().cities_overview()
        except FeatureStoreDown as exc:
            raise unavailable("Live data store", exc) from None
        except FeatureStoreBusy as exc:
            raise unavailable("Live data (being refreshed)", exc) from None

    @app.get("/forecast/{city}", response_model=Forecast)
    def forecast(city: str, group: Group = "general"):
        if city not in svc().cities:
            raise HTTPException(404, f"Unknown city '{city}'")
        try:
            result = svc().forecast(city, group)
            metrics.observe_forecast(city, result)
            return result
        except ModelServerError as exc:
            metrics.observe_forecast(city, None, error=True)
            raise unavailable("Model server", exc) from None
        except FeatureStoreDown as exc:
            raise unavailable("Live data store", exc) from None
        except FeatureStoreBusy as exc:
            raise unavailable("Live data (being refreshed)", exc) from None

    @app.get("/model")
    def model() -> dict:
        return svc().model

    @app.get("/guidance", response_model=list[Guidance])
    def guidance(category: Category, group: Group = "general"):
        try:
            return svc().guidance(category, group)
        except Exception as exc:  # noqa: BLE001 - index locked or model missing
            raise unavailable("Guidance index", exc) from None

    @app.get("/pipeline")
    def pipeline() -> dict:
        return state["pipeline"].get()

    @app.post("/alerts", include_in_schema=False)
    def alerts(payload: dict) -> dict:
        """Alertmanager's webhook: keep each alert's latest status for the console."""
        n = alert_store.receive(payload)
        log.info(
            "Received %d alert(s) from Alertmanager (%s)", n, payload.get("status")
        )
        return {"stored": n}

    @app.get("/metrics", include_in_schema=False)
    def prometheus_metrics() -> Response:
        body, content_type = metrics.render()
        return Response(body, media_type=content_type)

    return app


app = create_app()
