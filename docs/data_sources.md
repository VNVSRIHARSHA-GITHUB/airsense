# AirSense — Data Sources

This document describes every data source used by AirSense: what it contains, its format and update frequency, how it is accessed, its known issues, and its potential biases (AI Application Development Guidelines §II-A). Findings come from the exploratory analysis in `notebooks/01_eda.ipynb`.

---

## 1. Overview

| Source | Role in the pipeline | Access | Update frequency | Versioned with |
|---|---|---|---|---|
| CPCB historical data (Kaggle) | Training history (PM2.5) | Kaggle API (`kaggle datasets download`) | Static snapshot (2010-01-01 to 2023-03-31) | DVC (`data/raw/cpcb.dvc`) |
| CPCB real-time feed (data.gov.in) | Live inputs and "actual" measurements for monitoring | REST API, API key in `.env` | Hourly | Collected hourly; DVC versioning planned for Stage 9 |
| Open-Meteo weather | Weather features (training and serving) | REST API, no key | Hourly (forecast); archive back to 1940 | Snapshots saved under `data/raw/live/openmeteo/` |

All three sources measure or model the same quantities at the same locations, so the model sees consistent inputs during training and serving (design choice 1A: "CPCB measurements throughout").

---

## 2. CPCB historical data (Kaggle)

- **Dataset:** "Time Series Air Quality Data of India (2010–2023)", `abhisheksjha/time-series-air-quality-data-of-india-2010-2023`. Compiled from the Central Pollution Control Board (CPCB) CCR portal.
- **Download:** `kaggle datasets download -d abhisheksjha/time-series-air-quality-data-of-india-2010-2023 -p data/raw/cpcb --unzip`
- **Size:** 487 MB zipped, 1.6 GB unzipped; 453 station CSVs + `stations_info.csv`.
- **Licence:** see the dataset's Kaggle page.

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

### Cleaning rules derived from EDA (implemented in Stage 2)
1. Convert timestamps from IST to UTC.
2. Drop stations with no PM2.5 column.
3. Remove readings inside runs of ≥24 identical consecutive values (stuck sensors).
4. Keep readings equal to 1000 but flag them as possibly capped.
5. Average all stations in a city per hour to obtain the city-level PM2.5 series.

---

## 3. CPCB real-time feed (data.gov.in)

- **Dataset:** "Real time Air Quality Index from various locations", published by CPCB on the Open Government Data platform.
- **Endpoint:** `https://api.data.gov.in/resource/3b01bcb8-0b14-4abf-b6f2-c1bfd384ba69` (resource ID stored in `params.yaml`).
- **Authentication:** personal API key, stored only in `.env` (git-ignored); `.env.example` documents the variable name.
- **Licence:** Government Open Data License – India (as published on data.gov.in).
- **Client:** `src/airsense/ingestion/datagov_client.py`, run hourly (minute 5) by cron until Airflow takes over in Stage 9. Output: `data/raw/live/datagov/<UTC date>/<HHMM>.csv`.

### Format
- One record per **station × pollutant**: `country, state, city, station, last_update, latitude, longitude, pollutant_id, min_value, max_value, avg_value`. The collector adds `fetched_at_utc`.
- `last_update` is in IST, format `DD-MM-YYYY HH:MM:SS`.
- `min_value`, `max_value`, `avg_value` are delivered as **text** and must be converted to numbers.
- The client requests **PM2.5 only** (`filters[pollutant_id]=PM2.5`).

### Known issues
- **No history:** the feed returns only the current readings. Hours not collected (laptop off or asleep) cannot be retrieved later.
- **Varying station coverage:** the number of PM2.5 stations reporting changes from hour to hour (observed 371 to 525).
- **Data freshness:** readings lag about 30 minutes (`last_update` 16:00 IST fetched at 16:30 IST).
- **Meaning of `avg_value` is unconfirmed.** Example: Vijayawada min 12, max 305, avg 80 in one record — a spread suggesting the values cover a longer window (likely 24 hours, the period CPCB uses for the PM2.5 AQI) rather than one hour. To be verified from consecutive hourly snapshots before Stage 2.
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
| 8 | **Uncertain live `avg_value` window** | Live inputs may be smoother than hourly training data | To be resolved before Stage 2 |
| 9 | **Laptop-based live collection** | Hours when the laptop is off are permanently missing | Accepted for a course project; gaps handled in cleaning |
