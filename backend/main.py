"""AirSense backend API (Stage 6, D53).

Run from the project root:
    uvicorn main:app --app-dir backend --port 8000
Needs the MLflow model server running (model_server/serve.sh) for /forecast.

Endpoints (the API contract, documented in the LLD):
    GET /health              status of model server, feature store, champion version
    GET /cities              the 14 cities with the latest PM2.5 and its age
    GET /forecast/{city}     24 hourly forecasts + AQI, explanation, health guidance
    GET /model               the champion model and its metrics
    GET /guidance            advice for an AQI category and a user group
Interactive documentation: http://127.0.0.1:8000/docs
"""

from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

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
    guidance: Guidance | None = None
    warnings: list[str] = []
    model_latency_ms: float | None = None


class CityStatus(BaseModel):
    city: str
    region: str
    pm25_24h: float | None
    aqi_category: str | None
    age_hours: float | None


def create_app(service=None) -> FastAPI:
    """Build the app; tests pass a fake service, normal runs create the real one."""
    state = {"service": service}

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if state["service"] is None:
            from airsense.serving.service import ForecastService

            state["service"] = ForecastService()
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

    def svc():
        return state["service"]

    @app.get("/health")
    def health() -> dict:
        return svc().health()

    @app.get("/cities", response_model=list[CityStatus])
    def cities():
        return svc().cities_overview()

    @app.get("/forecast/{city}", response_model=Forecast)
    def forecast(city: str, group: Group = "general"):
        if city not in svc().cities:
            raise HTTPException(404, f"Unknown city '{city}'")
        return svc().forecast(city, group)

    @app.get("/model")
    def model() -> dict:
        return svc().model

    @app.get("/guidance", response_model=list[Guidance])
    def guidance(category: Category, group: Group = "general"):
        return svc().guidance(category, group)

    return app


app = create_app()
