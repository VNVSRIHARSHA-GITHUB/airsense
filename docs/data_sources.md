# AirSense — Data Sources

This document describes every data source used by AirSense: what it contains, its format and update frequency, how it is accessed, its known issues, and its potential biases (AI Application Development Guidelines §II-A). Findings come from the exploratory analysis in `notebooks/01_eda.ipynb`.

---

## 1. Overview

| Source | Role in the pipeline | Access | Update frequency | Versioned with |
|---|---|---|---|---|
| CPCB historical data (Kaggle) | Training history (PM2.5) | Kaggle API (`kaggle datasets download`) | Static snapshot (2010-01-01 to 2023-03-31) | DVC (`data/raw/cpcb.dvc`) |
| CPCB real-time feed (data.gov.in) | Live inputs and "actual" measurements for monitoring | REST API, API key in `.env` | Hourly | Collected every 30 min (cron); DVC versioning planned for Stage 9 |
| Open-Meteo weather | Weather features (training and serving) | REST API, no key | Hourly (forecast); archive back to 1940 | Snapshots saved under `data/raw/live/openmeteo/` |

All three sources measure or model the same quantities at the same locations, so the model sees consistent inputs during training and serving (design choice 1A: "CPCB measurements throughout").

---

## 2. CPCB historical data (Kaggle)

- **Dataset:** "Time Series Air Quality Data of India (2010–2023)", `abhisheksjha/time-series-air-quality-data-of-india-2010-2023`. Compiled from the Central Pollution Control Board (CPCB) CCR portal.
- **Download:** `kaggle datasets download -d abhisheksjha/time-series-air-quality-data-of-india-2010-2023 -p data/raw/cpcb --unzip`
- **Size:** 487 MB zipped, 1.6 GB unzipped; 453 station CSVs + `stations_info.csv`.
- **Licence:** **CC BY-NC-SA 4.0** (from the dataset's Kaggle metadata, `kaggle datasets metadata`). The underlying data comes from CPCB (cpcb.nic.in), compiled by the dataset author with Selenium.
  - **BY (attribution):** the dataset and CPCB must be credited — in this document, the report and the application's UI.
  - **NC (non-commercial):** use is limited to non-commercial purposes; a course project qualifies.
  - **SA (share-alike):** anything derived from it (e.g. the processed city-hour tables) may only be shared under the same licence.
  - In practice: the raw and processed data stay in DVC (not pushed to the public Git repository); only code and small metrics are in Git.

### Format
- **One CSV per monitoring station**, named by state code and station number (e.g. `AP001.csv`).
- **One row per hour:** `From Date`, `To Date` (e.g. `2016-07-01 10:00:00` → `11:00:00`), then 21 measurement columns:
  - Pollutants: `PM2.5 (ug/m3)`, `PM10`, `NO`, `NO2`, `NOx`, `NH3`, `SO2`, `CO`, `Ozone`, `Benzene`, `Toluene`, `Xylene`
  - Station weather: `Temp`, `RH`, `WS`, `WD`, `SR`, `BP`, `VWS`, `RF`, `AT`
- **`stations_info.csv`** maps each station to `state`, `city`, `agency` (e.g. CPCB, APPCB, DPCC), `station_location` and start date.
- **Timestamps are local Indian time (IST, UTC+05:30).** Confirmed in EDA: Delhi's daily PM2.5 minimum falls at 15:00–16:00 and peaks at night and around 08:00–09:00, consistent with IST. The pipeline converts them to UTC.

### EDA findings
| Property | Finding |
|---|---|
| Granularity | Hourly: 14,098,246 consecutive-row gaps are exactly 60 minutes; one station has a single 7-day gap |
| Date range | 2010-01-01 00:00 to 2023-03-31 23:00 |
| Rows | 14.1 M station-hours, 10.36 M with a PM2.5 value (**~26.5% missing**) |
| Stations without PM2.5 | 4 (DL018, KA005, KA006, TN007) — excluded |
| Value range | No negative or zero values; median 45.4, 99th percentile 367.8 µg/m³ |
| Capped values | Maximum is exactly **1000.0**; 292 readings equal 1000 (0.003%) |
| Stuck sensors | 31,452 readings (~0.3%) lie in runs of ≥24 identical consecutive values |
| Seasonality | Strong in the north and east (Delhi ~55 µg/m³ in August → ~220 in November; Kolkata similar); flat in the south (Chennai, Bengaluru 30–55 year-round) |

### Cleaning rules derived from EDA (implemented in Stage 2: `src/airsense/processing/process_history.py`)
1. Convert timestamps from IST to UTC.
2. Drop stations with no PM2.5 column.
3. Remove readings inside runs of ≥24 identical consecutive values (stuck sensors).
4. Keep readings equal to 1000 but flag them as possibly capped.
5. Compute each station's trailing 24-hour average (time-based window, at least 16 valid hours), then average the stations of each city per hour — the same quantity as the live feed (see §3).

**Results (first run):** raw validation kept 146 of 149 stations (DL018, KA005, KA006 quarantined: no PM2.5 column); 6.74 M station-hours processed; 10,186 stuck readings removed; 280 capped readings flagged. Coverage of the city 24-hour average since 2020: 98–100% for 13 cities, Pune 82.7%. Before ~2018 many cities rest on 1–2 patchy stations (e.g. Mumbai: 36% coverage over 2010–2023).

---

## 3. CPCB real-time feed (data.gov.in)

- **Dataset:** "Real time Air Quality Index from various locations", published by CPCB on the Open Government Data platform.
- **Endpoint:** `https://api.data.gov.in/resource/3b01bcb8-0b14-4abf-b6f2-c1bfd384ba69` (resource ID stored in `params.yaml`).
- **Authentication:** personal API key, stored only in `.env` (git-ignored); `.env.example` documents the variable name.
- **Licence:** Government Open Data License – India (GODL), as published on data.gov.in: free use, including derived works, with attribution to the source.
- **Client:** `src/airsense/ingestion/datagov_client.py`, run every 30 minutes (minutes 5 and 35) by cron until Airflow takes over in Stage 9. Output: `data/raw/live/datagov/<UTC date>/<HHMM>.csv`. Processed by `src/airsense/processing/process_live.py`.

### Format
- One record per **station × pollutant**: `country, state, city, station, last_update, latitude, longitude, pollutant_id, min_value, max_value, avg_value`. The collector adds `fetched_at_utc`.
- `last_update` is in IST, format `DD-MM-YYYY HH:MM:SS`.
- `min_value`, `max_value`, `avg_value` are delivered as **text** and must be converted to numbers.
- The client requests **PM2.5 only** (`filters[pollutant_id]=PM2.5`).

### Known issues
- **No history:** the feed returns only the current readings. Hours not collected (laptop off or asleep) cannot be retrieved later.
- **Varying station coverage:** the number of PM2.5 stations reporting changes from hour to hour (observed 371 to 525).
- **Data freshness:** readings lag about 30 minutes (`last_update` 16:00 IST fetched at 16:30 IST).
- **`avg_value`, `min_value`, `max_value` are rolling statistics, not hourly values.** Experiment `notebooks/03_avg_value_experiment.ipynb` (10 live hours, 151 stations): hourly values rejected by all four tests; the window length is bounded to 8–23 h (robust 8–25 h) from the live data alone; best simulated match 16 h, then 24 h; live min/max show the step shapes of a rolling window. Consistent with CPCB's 24-hour PM2.5 average over the available hours (at least 16 required). The training target is computed the same way (`decisions.md` D21). The exact window length is to be re-checked with ≥ 30 continuous hours.
- **Only the latest reading per station:** the feed holds each station's most recent reading; a station disappears from the feed until it publishes the next hour, so a snapshot only contains stations that have already published.
- **Publication lag:** readings usually appear within about an hour; the **midnight (00:00 IST) reading publishes slowly** (3 stations at 01:05, 69 at 01:31, 145 at 02:05 on 21 Oct 2026). Snapshots below 200 records are logged as *partial* (warning, not failure); collecting every 30 minutes catches late publishers, and repeated readings are removed in processing (485 repeats in the first 17 snapshots; no revised values seen).
- **Hour label:** `last_update` is assumed to mark the *end* of the 24-hour window, while the Kaggle history labels each hour by its *start*; live times are therefore shifted by −1 h (`params.yaml → live.time_offset_hours`). Not verifiable from the feed; the effect is at most 1/24 of a 24-hour window.
- **Freshness:** with the −1 h label and the publication lag, the newest live hour is normally about 2.5 h old; processed-data validation warns above 4 h.
- **CPCB's own caveat:** live data comes directly from field instruments without human checks and may contain errors or abnormal values.
- **API behaviour:** the gateway throttles the default `python-requests` User-Agent (HTTP 502 after ~60 s) and times out on large pages. The client sends an explicit User-Agent, uses 200-record pages, a 90 s timeout and retries with backoff (30 s, 60 s).

---

## 4. Open-Meteo weather

- **Endpoints:**
  - Forecast: `https://api.open-meteo.com/v1/forecast` — recent past days and the next days; used for serving.
  - Archive: `https://archive-api.open-meteo.com/v1/archive` — ERA5 reanalysis back to 1940; used for training.
- **Authentication:** none (free for non-commercial use).
- **Client:** `src/airsense/ingestion/openmeteo_client.py`; one request covers all cities.
- **Variables (hourly, UTC):** `temperature_2m`, `relative_humidity_2m`, `precipitation`, `wind_speed_10m`, `wind_direction_10m`, `surface_pressure`, `cloud_cover`.
- **Locations:** the coordinates in `params.yaml` (mean location of each city's live CPCB stations), so weather is taken where PM2.5 is measured.

### Known issues
- **Gridded model output, not station measurements:** values represent an area around the coordinates, not a specific street.
- **Archive vs. forecast:** training weather (reanalysis, close to what actually happened) differs in nature from serving weather (predicted). This is a mild training–serving skew, to be addressed in Stage 3.

---

## 5. Selected cities

14 cities chosen in EDA (`params.yaml → data.cities`):

| Region | Cities |
|---|---|
| North | Delhi, Lucknow, Patna, Kanpur, Jaipur, Chandigarh |
| West | Mumbai, Ahmedabad, Pune |
| South | Bengaluru, Chennai, Hyderabad |
| East / North-East | Kolkata, Guwahati |

**Criteria:**
1. ≥97% of hours covered (at least one station reporting) since 2020 — except Pune (83.9%), kept for western coverage.
2. ≥2 stations present in the live feed.
3. Spread across regions with different pollution behaviour (winter smog in the north, flat profiles in the south, monsoon effects in the west).
4. Excluded near-duplicates that share a larger city's air: Gurugram, Noida, Ghaziabad, Faridabad (Delhi region) and Howrah (Kolkata).

Kanpur and Chandigarh have only 2 live stations each; their live coverage will be monitored.

---

## 6. Potential biases and limitations

| # | Bias / limitation | Effect | Mitigation |
|---|---|---|---|
| 1 | **Unequal station density:** Delhi has 40 historical stations, Kanpur 4, Chandigarh 3 | City averages are more reliable for large cities; errors in small cities are dominated by single sensors | Track live stations per city; compare model error by city and region |
| 2 | **Station placement:** monitors are often near roads, industry or government sites | A city average may not represent residential exposure | Documented; the forecast is described as the average across a city's monitors |
| 3 | **Unequal history length:** Delhi from 2010, Chandigarh and Guwahati from 2019 | The model learns more seasonal cycles for some cities | `city` included as a feature; per-city evaluation |
| 4 | **Missing data (~26.5%) may not be random:** sensors can fail during extreme events | Extremes may be under-represented in training | Gaps handled in cleaning; coverage monitored |
| 5 | **Values capped at 1000 µg/m³** | Extreme peaks (e.g. Diwali) are underestimated | Rare (0.003%); flagged |
| 6 | **Station composition changes between 2023 and 2026:** Delhi had 40 stations in the historical data but 57 in the live feed | The city average changes because different sensors are averaged, not because air quality changed — this appears as drift | Monitored; retraining on newly collected live data |
| 7 | **3.5-year gap** between the end of history (March 2023) and live operation (October 2026) | Real changes since 2023 appear as data or concept drift | Drift detection and retraining (Stage 10) |
| 8 | **Live values are 24-hour rolling averages** (resolved) | Hourly training values would not match live inputs | Training target = 24-hour average computed like CPCB's (D21); exact window to re-check |
| 9 | **Laptop-based live collection** | Hours when the laptop is off are permanently missing (e.g. 02:05–18:24 IST on 21 Oct 2026) | Accepted for a course project; gaps handled in cleaning; freshness monitored |
| 10 | **Single-station hours** (Chandigarh, at times Kanpur) | The city value rests on one sensor | Live-station count checked in processed-data validation (warning) |

---

## 7. Processed outputs (Stage 2)

| Output | Produced by | Contents |
|---|---|---|
| `data/interim/raw_validation.json` | `validate_raw.py` | Per-station validation results; list of valid stations |
| `data/processed/station_hourly/` | `process_history.py` (Spark) | One row per station-hour: cleaned PM2.5, stuck/capped flags, 24-h average |
| `data/processed/city_hourly/` | `process_history.py` (Spark) | One row per city-hour: 24-h PM2.5 (the forecasting target), station counts |
| `data/processed/live_station_hourly/` | `process_live.py` (pandas) | One row per live station-hour: 24-h average, min, max, location |
| `data/processed/live_city_hourly/` | `process_live.py` (pandas) | Same columns as `city_hourly` |
| `data/interim/processed_validation.json` | `validate_processed.py` | Critical and warning results for both city tables |

All outputs are Parquet (partitioned by city) or JSON, produced by the DVC pipeline (`dvc.yaml`); times are UTC.
