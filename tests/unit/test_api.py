"""API tests with a fake service: endpoints, schemas, errors (Stage 6)."""

import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
from main import create_app


class FakeService:
    def __init__(self):
        self.cities = {"Delhi": {"region": "North"}}
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
            "guidance": None,
            "warnings": [],
            "model_latency_ms": 12.0,
        }

    def guidance(self, category, group="general"):
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


client = TestClient(create_app(FakeService()))


def test_forecast_returns_24_hours_and_schema():
    r = client.get("/forecast/Delhi")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True and len(body["forecast"]) == 24
    assert body["explanation"][0]["direction"] == "raises"


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
