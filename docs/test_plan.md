# AirSense — Test Plan and Test Cases

This document states what is tested in AirSense, at which level, with which tools,
and when a test counts as passed; it lists the test cases and the results of the
test round of 4–5 November 2026, including the defects found and how they were fixed.

Related: the system under test is described in [`architecture.md`](architecture.md)
and [`lld.md`](lld.md); the manual browser checks in full are in
[`ui_test_checklist.md`](ui_test_checklist.md).

---

## 1. Scope

**In scope**

| Area | What is tested |
|---|---|
| Data pipeline | collection, validation, processing (Spark and pandas), features, training-table construction |
| Model | training, baselines, quality gate, registration and the champion rule |
| Serving | the REST API (endpoints, schemas, status codes), the forecast logic, explanations, health advice |
| Web pages | the Forecast page (computer and phone) and the Pipeline console |
| Operations | containers, failure of each service, scheduled workflows, monitoring, alerts, drift detection, automatic retraining |

**Out of scope:** load testing beyond one laptop (the service handles a few requests
per second, measured forecast time about 30–70 ms); browsers other than Chrome and
Firefox; the accuracy of the official CPCB measurements themselves.

---

## 2. Approach: test levels

| Level | What it shows | Tool | When it runs | Pass criterion |
|---|---|---|---|---|
| **Unit** | each function does what it should on small, known inputs | pytest (162 tests: 153 quick, 9 with Spark) | every commit (pre-commit hook: quick tests, ruff lint and format check); Spark tests by hand | all pass; a failing test blocks the commit |
| **Data validation** | the real data is usable before it is used | Great Expectations and Python checks, as DVC stages | every pipeline run | no critical check fails (warnings are reported, not blocking) |
| **Model quality** | a new model is good enough to serve | quality gate (5 checks on validation data) and the champion rule | every training run | all 5 checks pass; the challenger replaces the champion only if ≥ 1 % better on the same rows |
| **Alert rules** | the alerts fire when they should and stay quiet otherwise | `promtool test rules` | after any rule change | all 7 scenarios pass |
| **Integration** | the containers work together on real data | `/health`, real requests | after every rebuild | all checks `true`, 24-hour forecast for every city with data |
| **System and failure** | the running system behaves well when a part fails, and recovers | stopping or crashing each part on the laptop; Prometheus, Alertmanager, console | after container or monitoring changes | the right message and alert; recovery without manual repair |
| **User interface** | layout, wording, keyboard use and failure messages, which only a person can judge | manual checklist in Chrome (39 checks) | before release | every check passes; defects are fixed and the check repeated |
| **Reproducibility** | someone else gets the same model from the repository | fresh clone, `dvc pull`, `dvc repro` | once, before release | byte-identical model |

**Test environment:** Ubuntu laptop, CPU only, 14 GB RAM; Python 3.12 in `.venv`;
Docker Compose with 8 containers; Airflow 3.3 in its own environment; Chrome with its
device toolbar at 400 px for the phone checks. The automated tests need no network,
no running services and no model download.

**Entry criteria for a test round:** containers up and healthy (`docker compose ps`),
Airflow running, live features less than 4 hours old, at least 5 GB free disk.

**Acceptance criteria** (a round passes when): all automated tests pass; every manual check passed, or failed and
was fixed and re-tested; no open defect that affects users or operation.

---

## 3. Automated test cases

### 3.1 Unit tests (`tests/unit/`, pytest)

| Area | File | Tests | What is checked |
|---|---|---|---|
| Collection | `test_datagov_client.py` | 6 | paging, retries, API key never in logs, empty feed gives exit code 99 and status "empty", network failure gives "error", files written only where intended |
| | `test_fetch_weather_history.py` | 5 | weather archive requests and the saved table |
| Validation | `test_validate_raw.py` | 4 | a clean station passes; negative values and repeated hours quarantine the station; a missing column; gaps and capped values only warn |
| | `test_validate_processed.py`, `test_validate_weather.py` | 9 | output columns and types, freshness and station-count warnings, broken data caught as critical |
| Processing | `test_process_history.py` | 9 | Spark: time zone shift, 24-hour average with the 16-hour rule, stuck sensors, city mean (marked `spark`) |
| | `test_process_live.py` | 6 | repeated readings removed, revised readings counted, hour convention, same columns as the history |
| Features | `test_build_features.py` | 14 | lags, changes, rolling values, weather at the middle of the hour, calendar in Indian time, Diwali window edges, identical files for identical data |
| | `test_build_training.py`, `test_build_training_live.py` | 10 | target and target-hour weather from *t + h*, features from *t*, no future data, missing targets dropped; live rows use only observed weather |
| | `test_feast_online.py` | 5 | online store refresh and freshness warning |
| Training | `test_train.py` | 13 | time split with gaps (no overlap across a boundary), baselines, metrics, live rows only in the training split, repeat factor, model signature with float inputs |
| | `test_gate.py` | 7 | each of the 5 gate checks passes and fails at its limit; empty forecasts counted correctly |
| Serving | `test_serving.py` | 7 | **serving rows equal training rows** for the same moment; missing inputs; AQI category edges; explanation names and totals |
| | `test_service.py` | 16 | normal forecast; stale, missing or NaN current value gives no forecast; unknown city; weather fallback; model server down; guidance failure keeps the forecast; exact measurement time in the city list; health: store reachable vs down |
| | `test_api.py` | 9 | every endpoint and response schema; 404 unknown city; 422 invalid value; 503 when a dependency is down (never 500) |
| | `test_guidance.py`, `test_settings.py` | 6 | all 18 advice texts parsed with sources; search returns the right category and group; settings and environment overrides |
| Console and monitoring | `test_pipeline.py` | 11 | steps and graph from `dvc.yaml`, status from `dvc status`, collection gaps (including a stopped collector), Airflow runs, monitoring summary |
| | `test_metrics.py`, `test_alerts.py` | 8 | `/metrics` content, free disk space, alerts stored as firing then resolved |
| | `test_record_forecasts.py`, `test_monitoring_checks.py`, `test_monitor_decision.py` | 11 | saved forecasts, forecast/actual matching (each forecast once), "error high" rule, PSI on equal and shifted data, retrain decision and cooldown |

Planted-bug checks: when the tests were written, deliberate bugs (a broken stuck-sensor
rule, de-duplication keeping the oldest reading, a wrong alert threshold) were each
caught by a failing test.

### 3.2 Alert-rule tests (`tests/monitoring/alert_rules_test.yml`, promtool)

| # | Scenario | Expected |
|---|---|---|
| 1 | backend not scraped | BackendDown pending, then firing after 2 min |
| 2 | more than 5 % server errors | HighErrorRate fires |
| 3 | slow forecasts with and without traffic | SlowForecasts fires only with ≥ 10 forecast requests |
| 4 | data age normal (2–3 h) and too old | LiveDataStale silent at normal age, fires above 4 h |
| 5 | CPCB feed empty for over an hour | CPCBFeedEmpty fires |
| 6 | free disk below 5 GB | DiskSpaceLow fires after 10 min |
| 7 | monitor reports error high and drift | ForecastErrorHigh and DataDrift fire |

### 3.3 Pipeline and model checks (every run)

| Check | Where | Expected |
|---|---|---|
| Raw data validation | DVC stage `validate_raw` | broken stations quarantined, run fails only if a city has no valid station |
| Processed and weather validation | `validate_processed`, `validate_weather` | no critical failure; warnings listed in the report |
| Quality gate | `gate` | 5 of 5 checks; a failure stops the pipeline before `register` |
| Champion rule | `register` | new model serves only if ≥ 1 % better on the same validation rows |
| Reproducibility | fresh clone (once) | same model file (identical md5) |

---

## 4. System and failure test cases

### 4.1 Containers (each part stopped or crashed with both pages open)

| ID | Action | Expected | Result |
|---|---|---|---|
| T1 | model server stopped | "Model server unavailable"; tiles still work; recovers on restart | Pass |
| T2 | Redis stopped | "Live data store unavailable" within a second; next request after restart works | Pass (after a fix: client retries made each request wait 7 s) |
| T3 | Qdrant stopped | forecast still shown; advice "not available" | Pass |
| T4 | backend crashed | restarted by Docker in ≈ 6 s, healthy in ≈ 22 s | Pass |
| T5 | backend stopped | red bar at once; nginx keeps serving the pages | Pass (after a fix: pages waited 60 s, then "HTTP 502") |
| T6 | whole system down and up | features and advice survive in Docker volumes | Pass |

### 4.2 Monitoring (laptop, 4–5 November 2026, IST)

| ID | Action | Expected | Result |
|---|---|---|---|
| A | model server stopped 3 min | ModelServerDown fires, console card red, resolves after restart | Pass: fired 23:43, resolved 23:46 |
| B | backend stopped 2 min | BackendDown fires; console banner | Pass: fired 23:54, resolved 23:56 |
| C | Airflow not running (laptop asleep) | CollectorNotRunning after 75 min + 5 min | Pass: fired 23:27 exactly as the rule says, resolved 23:45 |
| D | drift limit lowered to 0.25 | the monitor decides "retrain" and starts `airsense_retrain` itself; the champion rule decides | Pass: retrain green in 2 min 59 s, v1 kept, 3-day cooldown set |
| E | disk at about 2 GB during a rebuild (not planned) | — | Found: the model server could not start; led to the DiskSpaceLow alert (scenario 6 above) |

---

## 5. User-interface test cases

39 manual checks, run in Chrome on 5 November 2026 by Sri Harsha (steps and expected
results in [`ui_test_checklist.md`](ui_test_checklist.md)). Evidence: screenshots, and
for the console a script that prints the pipeline's own records next to it.

| Group | Checks | Passed first time | Failed, fixed, passed on re-test |
|---|---|---|---|
| Forecast page (computer) | F1–F18 | 16 | F1, F18 |
| Forecast page (phone, 400 px) | P1–P5 | 5 | — |
| Pipeline console | C1–C16 | 16 | — |
| **Total** | **39** | **37** | **2** |

Notes on how some checks were run:

- **F10** (stale data): instead of waiting 4 hours without collection, the age limit
  was lowered to 1 hour for a few minutes between two live runs, then restored.
- **F11, F12, C10, C14** (services down): covered by the monitoring tests A and B
  (§4.2); F12 was seen again on 5 November during a backend restart.
- **C3** "Overdue" when Airflow is stopped: covered by monitoring test C.
- **C4, C6–C9, C13**: console values compared with `dvc status`, the step reports,
  the snapshot files, the registry and gate reports, the collector log (no
  unredacted API key) and the monitor's reports; all matched.

---

## 6. Defects and findings

| ID | Found by | Description | Severity | Fix | Re-test |
|---|---|---|---|---|---|
| F1 | UI check F1 | "Latest measurement" showed 04:01 or 03:59 for a 04:00 reading: the page rebuilt the time from the age, which is rounded to 0.1 h | minor | `/cities` also returns the measurement time (`time_utc`); the header shows it; 2 new unit tests | Pass: "07:00 IST" = the value's hour |
| F18 | UI check F18 | keyboard users lost their place after choosing an advice group: the reload redrew the details and replaced the focused choice | moderate (accessibility) | focus returned to the chosen group after the reload | Pass |
| P-1 | console during C1/C4 | "2 of 15 steps need a re-run" with no real change: two code files had only been reformatted by the formatter, which changes their checksums | minor (would have re-run Spark and the training table for nothing) | `dvc commit` of the two steps (outputs unchanged), recorded in `dvc.lock` | Pass: "12 up to date" |
| O1 | UI checks F3, P4 | AQI band names sat on the line when it ran along a band edge | cosmetic | band name placed at the top or bottom of its band, away from the line | Pass |
| O2 | UI check F4 | brackets inside brackets in the advice source line | cosmetic | "…at Satisfactory levels: 31-60 µg/m³ (24-hour average)." | Pass |
| O3 | UI check F16 | "Satisfactory" wrapped, colour dot alone on one line | cosmetic | dot and name kept on one line | Pass |
| O4 | UI check F17 | the line crossed the "Peak" label when the measured value was above the peak | cosmetic | labels drawn with a halo | Pass |
| O5 | UI check P3 | phone city list had values without a unit ("Delhi: 360, Severe" could be read as an AQI index) | minor | "Delhi: 360 µg/m³, Severe" | Pass |
| O6 | UI check P4 | time labels kept their spacing when the screen became narrower without a reload | cosmetic | labels recomputed on resize | Pass |

Defects found earlier in development by the container and monitoring tests (T2, T5,
E above, and an alert that fired on single slow requests after a restart) were fixed
at the time and are covered by the tests listed in §3 and §4.

---

## 7. Summary

| Level | Cases | Result |
|---|---|---|
| Unit tests | 162 (153 quick + 9 Spark) | all pass |
| Alert-rule tests | 7 scenarios | all pass |
| Pipeline checks | validation, gate, champion rule on every run; reproducibility once | pass |
| Container failure tests | 6 | all pass (2 after fixes) |
| Monitoring failure tests | 4 planned + 1 found | all pass; the unplanned one led to a new alert |
| User-interface checks | 39 | 37 passed first time, 2 fixed and re-tested |

**Status:** acceptance criteria met; no open defect.

---

## 8. How to run the tests

```bash
pytest -m "not spark"          # quick unit tests (also run by the pre-commit hook)
pytest -m spark                # unit tests with a local Spark session
MLFLOW_TRACKING_URI=sqlite:///mlflow.db mlflow run . -e tests --env-manager local
                               # the same quick tests as an MLflow Project run
docker run --rm -v "$PWD":/w -w /w --entrypoint promtool prom/prometheus:v3.5.0 \
    test rules tests/monitoring/alert_rules_test.yml
curl -s http://127.0.0.1:8000/health    # integration: every check true
```

The manual checks: [`ui_test_checklist.md`](ui_test_checklist.md), with the result of
each check in its last column.
