"""Turn SHAP values of one forecast into a few plain-language factors (Stage 6).

SHAP values come from LightGBM's built-in TreeSHAP (pred_contrib) on the 24 input
rows; their mean over the 24 hours says which inputs pushed this city's forecast
up or down. In "change" mode they explain the predicted change from now.
"""

import numpy as np
import pandas as pd

WEATHER_LABELS = {
    "temperature_2m": "temperature",
    "relative_humidity_2m": "humidity",
    "precipitation": "rain",
    "wind_speed_10m": "wind speed",
    "surface_pressure": "air pressure",
    "cloud_cover": "cloud cover",
    "wind_dir_sin": "wind direction",
    "wind_dir_cos": "wind direction",
    "rain_24h": "rain in the last 24 h",
}
FIXED_LABELS = {
    "pm25_24h": "current PM2.5 level",
    "n_stations_24h": "number of reporting stations",
    "pm25_mean_72h": "average of the last 3 days",
    "pm25_max_72h": "peak of the last 3 days",
    "horizon": "hours ahead",
    "hour": "time of day",
    "day_of_week": "day of the week",
    "month": "season",
    "is_weekend": "weekend",
    "is_festival": "Diwali period",
    "city": "city",
    "region": "region",
}


def label(feature: str) -> str:
    """Plain-language name of a model feature."""
    if feature in FIXED_LABELS:
        return FIXED_LABELS[feature]
    if feature.startswith("pm25_lag_"):
        return f"PM2.5 {feature.removeprefix('pm25_lag_')} ago"
    if feature.startswith("pm25_change_"):
        return f"change over the last {feature.removeprefix('pm25_change_')}"
    for suffix, when in (("_target", "expected"), ("_now", "now")):
        if feature.endswith(suffix):
            base = feature.removesuffix(suffix)
            return f"{WEATHER_LABELS.get(base, base)} {when}"
    return feature


def top_factors(contrib: np.ndarray, features: list[str], n: int) -> list[dict]:
    """The n features with the largest mean |SHAP| over the rows, with direction.

    contrib: (rows, features + 1) from booster.predict(..., pred_contrib=True);
    the last column is the bias and is dropped.
    """
    mean = pd.Series(np.asarray(contrib)[:, :-1].mean(axis=0), index=features)
    # Merge the two wind-direction parts (sin/cos) into one factor.
    merged = mean.groupby([label(f) for f in features]).sum()
    top = merged.reindex(merged.abs().sort_values(ascending=False).index[:n])
    return [
        {
            "factor": name,
            "effect_ug_m3": round(float(v), 1),
            "direction": "raises" if v > 0 else "lowers",
        }
        for name, v in top.items()
    ]
