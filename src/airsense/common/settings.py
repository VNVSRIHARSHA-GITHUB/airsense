"""Project location and settings, shared by the code that runs in containers (Stage 8).

On the laptop the project folder is found from this file's location (editable
install). In a container the code is installed into the image, so the project
folder (mounted from the laptop) is given by the environment variable
AIRSENSE_ROOT.

A few settings differ between the laptop and the containers (how one service
reaches another); environment variables override them, everything else comes
from params.yaml:

    MODEL_SERVER_URL   -> serving.model_server_url   (e.g. http://model-server:5002)
    QDRANT_URL         -> guidance.qdrant_url         (e.g. http://qdrant:6333)
    REDIS_CONNECTION   -> Feast online store address  (e.g. redis:6379)
"""

import os
from pathlib import Path

import yaml

PROJECT_ROOT = Path(
    os.environ.get("AIRSENSE_ROOT") or Path(__file__).resolve().parents[3]
)

ENV_OVERRIDES = {
    "MODEL_SERVER_URL": ("serving", "model_server_url"),
    "QDRANT_URL": ("guidance", "qdrant_url"),
}


def load_params(root: Path = PROJECT_ROOT) -> dict:
    """params.yaml with the environment overrides applied."""
    with open(root / "params.yaml") as f:
        params = yaml.safe_load(f)
    for var, (block, key) in ENV_OVERRIDES.items():
        if os.environ.get(var):
            params.setdefault(block, {})[key] = os.environ[var]
    return params


def feature_store(root: Path = PROJECT_ROOT):
    """Feast store of feature_repo/, with the Redis address from REDIS_CONNECTION."""
    from feast import FeatureStore
    from feast.repo_config import load_repo_config

    repo = root / "feature_repo"
    config = load_repo_config(repo, repo / "feature_store.yaml")
    connection = os.environ.get("REDIS_CONNECTION")
    if connection and getattr(config.online_store, "type", "") == "redis":
        config.online_store.connection_string = connection
    return FeatureStore(repo_path=str(repo), config=config)
