"""Settings shared by laptop and containers: env overrides (Stage 8)."""

import shutil
from pathlib import Path

from airsense.common import settings

ROOT = Path(__file__).resolve().parents[2]


def test_params_without_overrides(monkeypatch):
    for var in settings.ENV_OVERRIDES:
        monkeypatch.delenv(var, raising=False)
    p = settings.load_params(ROOT)
    assert p["serving"]["model_server_url"] == "http://127.0.0.1:5002"
    assert p["guidance"]["qdrant_url"] == "http://localhost:6333"


def test_environment_overrides_addresses(monkeypatch):
    monkeypatch.setenv("MODEL_SERVER_URL", "http://model-server:5002")
    monkeypatch.setenv("QDRANT_URL", "http://qdrant:6333")
    p = settings.load_params(ROOT)
    assert p["serving"]["model_server_url"] == "http://model-server:5002"
    assert p["guidance"]["qdrant_url"] == "http://qdrant:6333"
    assert p["serving"]["top_factors"] == 4  # everything else from params.yaml


def test_feature_store_redis_address(tmp_path, monkeypatch):
    repo = tmp_path / "feature_repo"
    repo.mkdir()
    shutil.copy(ROOT / "feature_repo" / "feature_store.yaml", repo)
    monkeypatch.delenv("REDIS_CONNECTION", raising=False)
    assert settings.feature_store(tmp_path).config.online_store.connection_string == (
        "localhost:6379"
    )
    monkeypatch.setenv("REDIS_CONNECTION", "redis:6379")
    store = settings.feature_store(tmp_path)  # no connection is made here
    assert store.config.online_store.connection_string == "redis:6379"
