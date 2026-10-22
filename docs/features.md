# AirSense: Feature List

The columns of the training table (`data/training/h01.parquet … h24.parquet`, 43 columns) and how each is computed. This list feeds the HLD (feature section) and the model card. Design decisions: D31 (horizon as a feature), D35 and D37 (feature rules), D38 (time alignment), D39 (calendar), D40 (missing values), D41 (Feast).

**One row = one city, one forecast time *t*, one horizon *h*.** The model predicts the 24-hour average PM2.5 at *t + h*.

**Rule for every feature:** it must be computable at serving time from live data. PM2.5 features therefore use only the city's 24-hour average series (`pm25_24h`), because the live feed has no hourly values (D35).

---

## 1. Keys and target (not model inputs)

| Column | Type | Description |
|---|---|---|
| `time_utc` | timestamp (UTC) | Forecast time *t*: the PM2.5 hour the features describe (labelled at :30 UTC, e.g. 10:30 = the IST hour 16:00–17:00) |
| `target_time_utc` | timestamp (UTC) | *t + h*: the hour being predicted |
| `target` | float | **Target:** `pm25_24h` at *t + h*, in µg/m³ |

## 2. Model inputs (40)

### Identity and horizon (3)

| Feature | Type | Description |
|---|---|---|
| `city` | categorical | One of the 14 cities |
| `region` | categorical | North, West, South or East (from `params.yaml`); regional effects such as stubble burning in the north |
| `horizon` | integer 1–24 | Hours ahead (*h*) |

### PM2.5 at the forecast time *t* (14), from Feast view `pm_history` (training) / `pm_live` (serving)

| Feature | Description |
|---|---|
| `pm25_24h` | Current 24-hour average PM2.5 of the city (mean over its stations) |
| `n_stations_24h` | Number of stations behind the current value (reliability) |
| `pm25_lag_1h`, `_3h`, `_6h`, `_12h`, `_24h`, `_48h`, `_72h` | `pm25_24h` that many hours before *t* (7 features) |
| `pm25_change_1h`, `_6h`, `_24h` | `pm25_24h(t)` − `pm25_24h(t − k)`: direction and speed of change (3 features) |
| `pm25_mean_72h`, `pm25_max_72h` | Mean and maximum of `pm25_24h` over hours *t − 71 … t* (at least 24 hours present) |

### Weather now, at *t* (9), from Feast view `weather_history` (training) / forecast `past_days` (serving)

| Feature | Unit | Description |
|---|---|---|
| `temperature_2m_now` | °C | Air temperature at 2 m |
| `relative_humidity_2m_now` | % | Relative humidity at 2 m |
| `precipitation_now` | mm | Rain in the hour |
| `wind_speed_10m_now` | km/h | Wind speed at 10 m (wind disperses pollution) |
| `surface_pressure_now` | hPa | Surface pressure (high pressure: stagnant air) |
| `cloud_cover_now` | % | Total cloud cover |
| `wind_dir_sin_now`, `wind_dir_cos_now` | – | Wind direction as sine and cosine, so 0° and 360° are the same |
| `rain_24h_now` | mm | Rain over the last 24 hours (washout) |

### Weather at the target hour *t + h* (9)

The same 9 variables with the suffix `_target`. Training: archive weather (what happened). Serving: the forecast for that hour (D34: a documented training/serving skew, monitored in Stage 10).

### Calendar of the target hour *t + h*, in Indian time (5)

| Feature | Description |
|---|---|
| `hour` | Hour of day in IST (0–23); a row at 10:30 UTC is hour 16 |
| `day_of_week` | Monday = 0 … Sunday = 6 (IST) |
| `month` | 1–12 (IST) |
| `is_weekend` | 1 on Saturday and Sunday |
| `is_festival` | 1 within ±2 days of Diwali (IST dates, `features.diwali_dates`) |

---

## 3. How the features are produced

| Step | Code | Notes |
|---|---|---|
| Hourly grid | `complete_hourly_grid()` | One row per city per hour, so a lag of *k* means *k* hours, even across gaps |
| PM2.5 features | `pm_features()` | Only values at or before *t* (leakage test in `tests/unit/test_build_features.py`) |
| Weather features | `weather_features()` | Relabelled −30 min to the PM2.5 hour (D38) |
| Calendar | `calendar_features()` | Computed when the training set is built and at serving time; not stored |
| Training rows | `build_training.py` | Feast point-in-time join for *t*; the same result looked up at *t + h* for target and target weather; rows without a target dropped (D40) |

All lags and windows come from `params.yaml → features`; the newest features need 72 hours of history (`min_history_hours()`), which a unit test confirms gives the same values as the full history.

## 4. Missing values

Missing feature values (station outages, the start of each city's history) are left empty; LightGBM handles them natively (D40). Rows without a target are not used.
