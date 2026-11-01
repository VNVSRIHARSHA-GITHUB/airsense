"""Turn SHAP values of one forecast into plain-language factors (Stage 6).

SHAP values come from LightGBM's built-in TreeSHAP (pred_contrib) on the 24 input
rows; their mean over the 24 hours says which inputs pushed this city's forecast
up or down. In "change" mode they explain the predicted change from now.

Inputs whose value is missing for this forecast are not listed as factors (the
model still gives them an effect, but "PM2.5 6 hours ago raises the forecast"
would be misleading when that reading does not exist); their effect is counted
in "all other factors combined", so the listed factors plus the rest add up.
"""

import numpy as np
import pandas as pd

WEATHER_LABELS = {
    "temperature_2m": "temperature",
    "relative_humidity_2m": "humidity",
    "precipitation": "hourly rain",
    "wind_speed_10m": "wind speed",
    "surface_pressure": "air pressure",
    "cloud_cover": "cloud cover",
    "wind_dir_sin": "wind direction",
    "wind_dir_cos": "wind direction",
}
FIXED_LABELS = {
    "pm25_24h": "current PM2.5 level",
    "n_stations_24h": "number of reporting stations",
    "pm25_mean_72h": "average of the last 3 days",
    "pm25_max_72h": "peak of the last 3 days",
    "rain_24h_now": "rain in the last 24 hours",
    "rain_24h_target": "expected rain (24-hour total)",
    "horizon": "hours ahead",
    "hour": "time of day",
    "day_of_week": "day of the week",
    "month": "season",
    "is_weekend": "weekend",
    "is_festival": "Diwali period",
    "city": "this city's usual level",
    "region": "this region's usual level",
}


def _hours(text: str) -> str:
    """'6h' -> '6 hours', '1h' -> '1 hour'."""
    n = text.removesuffix("h")
    return f"{n} hour" if n == "1" else f"{n} hours"


def label(feature: str) -> str:
    """Plain-language name of a model feature."""
    if feature in FIXED_LABELS:
        return FIXED_LABELS[feature]
    if feature.startswith("pm25_lag_"):
        return f"PM2.5 {_hours(feature.removeprefix('pm25_lag_'))} ago"
    if feature.startswith("pm25_change_"):
        return f"change over the last {_hours(feature.removeprefix('pm25_change_'))}"
    if feature.endswith("_target"):
        base = feature.removesuffix("_target")
        return f"expected {WEATHER_LABELS.get(base, base)}"
    if feature.endswith("_now"):
        base = feature.removesuffix("_now")
        return f"{WEATHER_LABELS.get(base, base)} now"
    return feature


def explain(
    contrib: np.ndarray,
    features: list[str],
    n: int,
    missing: set[str] | frozenset[str] = frozenset(),
) -> dict:
    """The n factors with the largest mean |SHAP| over the rows, and the rest.

    contrib: (rows, features + 1) from booster.predict(..., pred_contrib=True);
    the last column is the bias and is dropped. missing: features with no value
    in this forecast; a factor is left out when all its features are missing.
    Returns {"factors": [...], "other_ug_m3": effect of everything not listed}.
    """
    mean = pd.Series(np.asarray(contrib)[:, :-1].mean(axis=0), index=features)
    labels = [label(f) for f in features]
    # Merge the parts of one factor (wind direction sin/cos) into one.
    merged = mean.groupby(labels).sum()
    present = {lab for f, lab in zip(features, labels, strict=True) if f not in missing}
    candidates = merged[merged.index.isin(present)]
    top = candidates.reindex(candidates.abs().sort_values(ascending=False).index[:n])
    other = float(merged.sum() - top.sum())
    return {
        "factors": [
            {
                "factor": name,
                "effect_ug_m3": round(float(v), 1),
                "direction": "raises" if v > 0 else "lowers",
            }
            for name, v in top.items()
        ],
        "other_ug_m3": round(other, 1),
    }


def top_factors(contrib: np.ndarray, features: list[str], n: int) -> list[dict]:
    """Only the listed factors (kept for callers that need no "other" total)."""
    return explain(contrib, features, n)["factors"]


def missing_note(empty: list[str]) -> str:
    """A visitor-facing note for PM2.5 history that is missing in this forecast."""
    names = [label(f) for f in empty]
    listed = (
        names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
    )
    return f"Some past readings are missing ({listed}), so this forecast may be less accurate."
