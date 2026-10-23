"""Feast feature definitions for AirSense (Stage 3.5).

One entity (city) and three feature views over the tables written by
src/airsense/features/build_features.py:

- pm_history       PM2.5 features from the history (training, offline store);
- weather_history  weather features from the archive (training, offline store);
- pm_live          PM2.5 features from the live feed (serving, online store).

pm_history and pm_live share ONE schema (pm_schema), so training and serving
request the same feature names. Requested with full_feature_names=False, both
return plain column names (e.g. pm25_lag_24h), so the model never sees a
difference. Lag and window columns are generated from params.yaml, the same
settings build_features.py uses.

Source paths point at the Parquet files below each folder; DuckDB (the offline
store) reads the city=<name> sub-folders back as the city column.

Serving-side weather (forecast for the next 24 hours) is not stored here: it is
keyed by target hour, not by city alone, and is fetched at request time (Stage 6).
"""

from datetime import timedelta
from pathlib import Path

import yaml
from feast import Entity, FeatureService, FeatureView, Field, FileSource, ValueType
from feast.data_format import ParquetFormat
from feast.types import Float64

REPO = Path(__file__).resolve().parent
FEATURES = REPO.parent / "data" / "features"
with open(REPO.parent / "params.yaml") as f:
    CFG = yaml.safe_load(f)["features"]

city = Entity(
    name="city",
    join_keys=["city"],
    value_type=ValueType.STRING,
    description="One of the 14 configured cities (params.yaml -> data.cities)",
)

W, RW = CFG["rolling_hours"], CFG["rain_window_hours"]
PM_COLUMNS = [
    "pm25_24h",
    "n_stations_24h",
    *[f"pm25_lag_{k}h" for k in CFG["lags_hours"]],
    *[f"pm25_change_{k}h" for k in CFG["change_hours"]],
    f"pm25_mean_{W}h",
    f"pm25_max_{W}h",
]
WEATHER_COLUMNS = [
    "temperature_2m",
    "relative_humidity_2m",
    "precipitation",
    "wind_speed_10m",
    "surface_pressure",
    "cloud_cover",
    "wind_dir_sin",
    "wind_dir_cos",
    f"rain_{RW}h",
]


def pm_schema() -> list[Field]:
    """The PM2.5 feature schema, shared by the history and live views."""
    return [Field(name=c, dtype=Float64) for c in PM_COLUMNS]


def source(name: str) -> FileSource:
    """A feature table written by build_features.py (Parquet, partitioned by city)."""
    return FileSource(
        name=f"{name}_source",
        # All Parquet files below the folder; DuckDB reads the city=<name>
        # sub-folders back as the city column (hive partitioning).
        path=str(FEATURES / name / "**" / "*.parquet"),
        file_format=ParquetFormat(),
        timestamp_field="time_utc",
    )


# ttl: the point-in-time join takes the newest row at or before the requested
# time, but not older than this. Every table has one row per hour (hourly grid),
# so 1 hour finds the exact row and never silently uses an older one.
pm_history = FeatureView(
    name="pm_history",
    entities=[city],
    ttl=timedelta(hours=1),
    schema=pm_schema(),
    source=source("pm_history"),
    online=False,  # training only
    description="PM2.5 features from the CPCB history (training)",
)
weather_history = FeatureView(
    name="weather_history",
    entities=[city],
    ttl=timedelta(hours=1),
    schema=[Field(name=c, dtype=Float64) for c in WEATHER_COLUMNS],
    source=source("weather_history"),
    online=False,
    description="Archive weather relabelled to the PM2.5 hour (training)",
)
pm_live = FeatureView(
    name="pm_live",
    entities=[city],
    # Feast does not apply ttl to online reads, so the freshness of the online
    # values is checked by src/airsense/features/feast_online.py instead.
    ttl=timedelta(hours=1),
    schema=pm_schema(),
    source=source("pm_live"),
    online=True,  # materialized to the online store for serving
    description="PM2.5 features from the live data.gov.in feed (serving)",
)

training_features = FeatureService(
    name="training_features", features=[pm_history, weather_history]
)
serving_features = FeatureService(name="serving_features", features=[pm_live])
