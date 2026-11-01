"""API tests with a fake service: endpoints, schemas, errors (Stage 6)."""

import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
from main import create_app

from airsense.serving.service import (
    FeatureStoreBusy,
    FeatureStoreDown,
    ModelServerError,
)


class FakeService:
    def __init__(self):
        self.cities = {
            "Delhi": {"region": "North"},
            "Pune": {"region": "West"},
            "Patna": {"region": "East"},
        }
        self.model = {"name": "airsense-pm25", "version": "1", "target_mode": "change"}

    def health(self):
        return {"status": "ok", "checks": {"model_server": True}}

    def cities_overview(self):
        return [
            {
                "city": "Delhi",
                "region": "North",
                "pm25_24h": 180.0,
                "aqi_category": "Very Poor",
                "age_hours": 2.4,
            }
        ]

    def forecast(self, city, group="general"):
        if group == "outdoor_workers":
            raise ModelServerError("connection refused")
        if city == "Pune":
            raise FeatureStoreBusy("online store read failed")
        if city == "Patna":
            raise FeatureStoreDown("Error 111 connecting to redis:6379")
        if group == "sensitive":
            return {
                "city": city,
                "region": "North",
                "generated_at_utc": "x",
                "available": False,
                "reason": "no current PM2.5 value",
                "model": self.model,
            }
        hour = {
            "horizon": 1,
            "time_utc": "t",
            "time_ist": "Fri 23 Oct 18:00",
            "pm25": 182.0,
            "aqi_category": "Very Poor",
        }
        return {
            "city": city,
            "region": "North",
            "generated_at_utc": "x",
            "available": True,
            "model": self.model,
            "current": {
                "pm25_24h": 180.0,
                "aqi_category": "Very Poor",
                "time_utc": "t",
                "time_ist": "s",
                "age_hours": 2.4,
                "stations": 40,
            },
            "forecast": [hour] * 24,
            "worst_next_24h": hour,
            "explanation": [
                {
                    "factor": "wind speed expected",
                    "effect_ug_m3": 8.5,
                    "direction": "raises",
                }
            ],
            "explanation_other_ug_m3": -1.2,
            "guidance": None,
            "warnings": [],
            "model_latency_ms": 12.0,
        }

    def guidance(self, category, group="general"):
        if group == "outdoor_workers":
            raise RuntimeError("storage folder is already accessed")
        return [
            {
                "category": category,
                "pm25_range": "r",
                "group": group,
                "text": "advice",
                "sources": ["CPCB"],
                "score": 0.9,
            }
        ]


class FakePipeline:
    def get(self):
        return {"stages": [{"name": "train", "status": "up to date"}], "edges": []}


client = TestClient(create_app(FakeService(), FakePipeline()))


def test_forecast_returns_24_hours_and_schema():
    r = client.get("/forecast/Delhi")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True and len(body["forecast"]) == 24
    assert body["explanation"][0]["direction"] == "raises"
    assert body["explanation_other_ug_m3"] == -1.2  # not dropped by the response model


def test_unknown_city_is_404():
    assert client.get("/forecast/Atlantis").status_code == 404


def test_unavailable_forecast_has_a_reason():
    body = client.get("/forecast/Delhi", params={"group": "sensitive"}).json()
    assert body["available"] is False and body["reason"]


def test_invalid_group_or_category_is_rejected():
    assert (
        client.get("/forecast/Delhi", params={"group": "everyone"}).status_code == 422
    )
    assert client.get("/guidance", params={"category": "Bad"}).status_code == 422


def test_other_endpoints():
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/cities").json()[0]["city"] == "Delhi"
    assert client.get("/model").json()["version"] == "1"
    assert client.get("/guidance", params={"category": "Poor"}).json()[0]["text"]


def test_backend_failures_are_503_not_500():
    r = client.get("/forecast/Delhi", params={"group": "outdoor_workers"})
    assert r.status_code == 503 and "Model server" in r.json()["detail"]
    r = client.get("/guidance", params={"category": "Poor", "group": "outdoor_workers"})
    assert r.status_code == 503


def test_pipeline_endpoint():
    body = client.get("/pipeline").json()
    assert body["stages"][0]["status"] == "up to date"


def test_feature_store_refresh_is_503_not_500():
    r = client.get("/forecast/Pune")
    assert r.status_code == 503 and "being refreshed" in r.json()["detail"]


def test_feature_store_down_is_503_with_its_own_message():
    r = client.get("/forecast/Patna")
    assert r.status_code == 503 and "Live data store" in r.json()["detail"]
    assert "being refreshed" not in r.json()["detail"]
