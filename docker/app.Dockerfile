# AirSense application image (Stage 8): one image for both the backend API and
# the MLflow model server; docker-compose.yml picks the command for each.
# Only code and packages are inside. Data, models (mlruns/, mlflow.db), params
# and the Feast repo are mounted from the project folder at run time, so a new
# champion model needs a container restart, not a rebuild.
#
# Build from the project root:
#   docker build -f docker/app.Dockerfile -t airsense-app:$(git rev-parse --short HEAD) .

# ---------- stage 1: install packages into a virtual environment ----------
FROM python:3.12-slim-bookworm AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
COPY requirements-serve.txt requirements.txt /tmp/req/
# requirements.txt as constraints: every indirect package gets the same version
# as in the laptop's venv, but only what serving needs is installed.
RUN pip install -r /tmp/req/requirements-serve.txt -c /tmp/req/requirements.txt
COPY pyproject.toml /app/
COPY src /app/src
RUN pip install --no-deps /app

# ---------- stage 2: the image that runs ----------
FROM python:3.12-slim-bookworm
# libgomp1: OpenMP runtime LightGBM needs; git: lets `dvc status` read the repo
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 git \
    && rm -rf /var/lib/apt/lists/*
# Same user id as the laptop user, so files on the mounted folder are readable
# and anything written to the writable mounts belongs to that user.
ARG UID=1000
RUN useradd --create-home --uid ${UID} airsense \
    && install -d -o airsense -g airsense /home/airsense/.cache
# (.cache must exist and belong to airsense before the Hugging Face cache is
#  mounted inside it; otherwise Docker creates it as root and nothing else,
#  e.g. matplotlib, can write there)
COPY --from=builder /opt/venv /opt/venv
COPY backend /app/backend
COPY model_server /app/model_server
# Fail the build, not the running container, if a module loaded at run time is
# missing. Some are imported only when used (e.g. Feast's DuckDB offline store
# when feature_store.yaml is read), so a plain `import feast` would not notice.
RUN /opt/venv/bin/python -c "import airsense.serving.service, airsense.serving.pipeline, \
    airsense.guidance.index, mlflow.lightgbm, qdrant_client, dvc.cli, \
    sentence_transformers, feast.infra.online_stores.redis, \
    feast.infra.offline_stores.duckdb"
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HUB_OFFLINE=1 \
    MLFLOW_DISABLE_AGENT_HINT=1
USER airsense
WORKDIR /app
EXPOSE 8000 5002
# Default command: the backend. The model server overrides it in docker-compose.yml.
CMD ["uvicorn", "main:app", "--app-dir", "backend", "--host", "0.0.0.0", "--port", "8000"]
