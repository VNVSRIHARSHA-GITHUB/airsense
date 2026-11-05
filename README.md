# AirSense

**24-hour PM2.5 forecasts for 14 Indian cities, with the reasons behind each forecast
and health advice: built and operated as an end-to-end AIOps system.**

AirSense collects the official CPCB air-quality feed every 30 minutes, forecasts the
24-hour average PM2.5 for each of the next 24 hours, explains what drives the
forecast, and tells people what to do about it (for the general public, sensitive
groups and outdoor workers). Behind the web page, the whole life cycle is automated:
versioned data, validated pipelines, tracked experiments, a quality gate and a model
registry, containers, scheduled workflows, monitoring with alerts, drift detection
and automatic retraining.

DA3408 course project · Sri Harsha (DA24B034) · November 2026

![Forecast page](docs/images/manual_forecast_desktop.png)

---

## Architecture

![Architecture](docs/images/architecture.png)

- **Pipeline** (on the laptop, run by DVC, scheduled by Airflow): collect → validate
  → process (Spark for the 14-year history) → features (Feast) → train (LightGBM,
  MLflow) → quality gate → registry.
- **Serving** (Docker Compose): nginx serves the pages and forwards `/api` to a
  FastAPI backend, which reads live features from Redis, calls the MLflow model
  server, explains the forecast with SHAP and finds advice in Qdrant.
- **Monitoring** (Docker Compose + an hourly workflow): Prometheus, Alertmanager and
  Grafana watch the service, the live data and the model; every served forecast is
  compared with what was measured; drift in the inputs or a high error starts a
  retrain by itself.

Details: [docs/architecture.md](docs/architecture.md).

## AIOps practices, and where they are

| Practice | Tool | Where |
|---|---|---|
| Data and model versioning | DVC (15-stage pipeline, local remote) | `dvc.yaml`, `dvc.lock`, `data/**/*.dvc` |
| Data validation | Great Expectations, Python checks | `src/airsense/validation/` |
| Large-scale processing | PySpark | `src/airsense/processing/process_history.py` |
| Feature store | Feast (DuckDB offline, Redis online) | `feature_repo/`, `src/airsense/features/` |
| Experiment tracking, model registry | MLflow (champion / challenger) | `src/airsense/training/` |
| Quality gate | five checks against baselines | `src/airsense/training/gate.py`, `params.yaml` |
| Explainability | SHAP (LightGBM contributions) | `src/airsense/serving/explain.py` |
| Retrieval for advice | sentence-transformers + Qdrant | `src/airsense/guidance/`, `guidance_docs/` |
| Tests and pre-commit | pytest (162 tests), ruff, promtool | `tests/`, `.githooks/pre-commit` |
| Packaging and serving | Docker Compose (8 containers), FastAPI, MLflow model server, nginx | `docker-compose.yml`, `docker/`, `backend/` |
| Orchestration | Apache Airflow 3.3 (4 workflows) | `airflow/dags/` |
| Monitoring and alerting | Prometheus (13 alert rules), Alertmanager, Grafana | `docker/monitoring/`, `src/airsense/serving/metrics.py` |
| Model monitoring | forecast vs actual, drift (PSI), automatic retrain | `src/airsense/monitoring/`, `airflow/dags/airsense_monitor.py` |
| Operator console | Pipeline console with five tabs | `frontend/pipeline.html` |

## Repository layout

| Folder | Contents |
|---|---|
| `src/airsense/` | the Python package: ingestion, validation, processing, features, training, serving, guidance, monitoring (and `common` for settings) |
| `backend/` | FastAPI application (`main.py`) |
| `frontend/` | Forecast page and Pipeline console (HTML, CSS, JavaScript, Chart.js) |
| `model_server/` | start script for the MLflow model server |
| `feature_repo/` | Feast feature definitions and store settings |
| `airflow/` | the four workflows (`dags/`) and the scripts that start Airflow |
| `docker/` | Dockerfiles, nginx, Prometheus, Alertmanager and Grafana configuration |
| `guidance_docs/` | health advice per AQI category and group (with sources) |
| `tests/` | unit tests (`unit/`) and alert-rule tests (`monitoring/`) |
| `notebooks/` | exploratory analysis: EDA, live-feed checks, the 24-hour average experiment |
| `docs/` | architecture, low-level design, user manual, data sources, features, UI checklist |
| `data/`, `models/`, `reports/` | data, model and plots made by the pipeline, versioned with DVC; Git holds only the `.dvc` files, `dvc.lock` and the small metric files in `data/interim/` |

Top-level files: `dvc.yaml` (the pipeline), `params.yaml` (every setting),
`docker-compose.yml`, `pyproject.toml`, and `requirements.txt` (laptop),
`requirements-serve.txt` (containers), `requirements-dev.txt` (notebooks).
Created when the system runs and kept out of Git: `logs/` (collector, Airflow
runs, alerts), `mlruns/` and `mlflow.db` (MLflow), `feature_repo/data/` (Feast registry).

## Quick start

**Needs:** Ubuntu (or another Linux), Python 3.12, Docker with Compose, about 15 GB
free disk, a free [data.gov.in](https://data.gov.in) API key and a Kaggle account.

```bash
# 1. environment
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt && pip install -e .
git config core.hooksPath .githooks            # lint, format and tests before each commit
cp .env.example .env                           # then fill in DATA_GOV_IN_API_KEY and GRAFANA_ADMIN_PASSWORD

# 2. data and model (history from Kaggle; everything else is built by DVC)
kaggle datasets download -d abhisheksjha/time-series-air-quality-data-of-india-2010-2023 \
    -p data/raw/cpcb --unzip
python src/airsense/ingestion/datagov_client.py   # a first live snapshot (Airflow collects the rest)
docker compose up -d redis qdrant              # stores the pipeline fills (online features, advice)
dvc repro                                      # validate, process, features, train, gate, register

# 3. serving and monitoring
mkdir -p logs/alerts
AIRSENSE_TAG=$(git rev-parse --short HEAD) docker compose up -d --build

# 4. scheduler (own terminal; installed once, see docs/architecture.md §8)
bash airflow/start.sh
```

| Open | Address |
|---|---|
| Forecast page | http://localhost:8080 |
| Pipeline console | http://localhost:8080/pipeline.html |
| API documentation | http://127.0.0.1:8000/docs |
| Airflow | http://localhost:8081 |
| Grafana / Prometheus / Alertmanager | http://localhost:3000 / :9090 / :9093 |

Everyday operation, failure handling and recovery: [docs/architecture.md](docs/architecture.md) §7–8
and the [user manual](docs/user_manual.md).

## Tests

```bash
pytest -m "not spark"          # 153 quick tests (also run by the pre-commit hook)
pytest -m spark                # 9 tests with a local Spark session
docker run --rm -v "$PWD":/w -w /w --entrypoint promtool prom/prometheus:v3.5.0 \
    test rules tests/monitoring/alert_rules_test.yml   # alert rules, 7 scenarios
```

Test plan, test cases and results: [docs/test_plan.md](docs/test_plan.md); manual checks of both web pages: [docs/ui_test_checklist.md](docs/ui_test_checklist.md).
Failure tests (each container stopped, alerts, automatic retraining):
[docs/architecture.md](docs/architecture.md) §7.

## Results

| | Value |
|---|---|
| Training data | 14.3 million rows (city × hour × 24 horizons), 2018–2023, 40 inputs |
| Test error (Oct 2022 – Mar 2023) | MAE **6.80 µg/m³**, against 8.59 for "no change": **21 %** better |
| AQI category right | about 83 % of test forecasts |
| Live check (3 days to 5 November 2026) | model 10.4 vs "no change" 11.7 µg/m³ over 798 forecast/actual pairs (10 % better) |
| Failure tests | model server, backend and Airflow outages each raised the right alert and recovered; drift started a retrain without a person |

## Documentation

| Document | Contents |
|---|---|
| [docs/architecture.md](docs/architecture.md) | components, data flow, deployment, workflows, monitoring, failure behaviour, running it, limitations |
| [docs/hld.md](docs/hld.md) | design choices: what was chosen, the alternatives and why |
| [docs/lld.md](docs/lld.md) | API contract, request flow, model input contract, error handling, metrics, configuration, tests |
| [docs/user_manual.md](docs/user_manual.md) | how to use the Forecast page and the Pipeline console; what to do when an alert fires |
| [docs/data_sources.md](docs/data_sources.md) | sources, formats, licences, processed outputs |
| [docs/features.md](docs/features.md) | every model input and how it is computed |
| [docs/test_plan.md](docs/test_plan.md) | test plan: levels, tools, pass criteria; all test cases and the results of the test round |
| [docs/ui_test_checklist.md](docs/ui_test_checklist.md) | manual checks of the web pages, with results |

## Limitations

- **Collection only while the laptop runs** (cloud services were not allowed): hours
  when it is off are missing for good, because the live feed has no history.
- **Single machine:** no replication; the DVC backup is on the same disk; Grafana and
  Prometheus are local to the laptop; alerts go to the console only.
- **Little live data in training so far:** retrained models have not yet beaten the
  first model by the required 1 %.

Possible extensions: e-mail alerts, starting Airflow with the laptop, a map view.

## Data and licences

- Historical PM2.5: CPCB (cpcb.nic.in), via the Kaggle dataset
  *Time Series Air Quality Data of India (2010–2023)*, **CC BY-NC-SA 4.0**; not
  stored in this repository (DVC only).
- Live PM2.5: CPCB real-time feed on data.gov.in, Government Open Data License – India.
- Weather: [Open-Meteo.com](https://open-meteo.com), **CC BY 4.0**.
- Chart.js (MIT), included in `frontend/vendor/`.

Full attribution: [docs/data_sources.md](docs/data_sources.md).
