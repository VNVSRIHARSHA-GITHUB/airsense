"""Build the 24 model input rows for one city, exactly as in training (Stage 6).

Training (build_training.py) takes, for a forecast time t and each horizon h:
  PM2.5 features at t (Feast), weather at t (`_now`), weather at t + h (`_target`),
  the calendar of t + h in Indian time, horizon, city and region codes.
Serving does the same from live inputs:
  PM2.5 features from Feast's ONLINE store, weather from the Open-Meteo forecast
  run through the same weather_features(), the same calendar_features().
A unit test checks that both paths give identical rows for the same inputs.
"""

import numpy as np
import pandas as pd

from airsense.features.build_features import calendar_features

AQI_EDGES = [30, 60, 90, 120, 250]  # CPCB 24-h PM2.5 bands (ug/m3), as in train.py
AQI_NAMES = ["Good", "Satisfactory", "Moderate", "Poor", "Very Poor", "Severe"]


def aqi_name(value: float) -> str | None:
    """CPCB AQI category of a 24-hour PM2.5 value (None if missing)."""
    if value is None or not np.isfinite(value):
        return None
    return AQI_NAMES[int(np.searchsorted(AQI_EDGES, value, side="left"))]


def build_rows(
    pm: dict[str, float],
    t: pd.Timestamp,
    weather: pd.DataFrame,
    weather_cols: list[str],
    city_code: int,
    region_code: int,
    cal_cfg: dict,
    feature_order: list[str],
    horizons: int = 24,
) -> tuple[pd.DataFrame, pd.DatetimeIndex, int]:
    """Model input rows for h = 1 .. horizons.

    pm:      PM2.5 feature values at t (from Feast online), by feature name;
    t:       forecast time (UTC-aware), the PM2.5 hour the features describe;
    weather: output of weather_features() for this city (time_utc UTC-aware,
             already relabelled to the PM2.5 hour convention: weather at 11:00 describes the hour labelled 10:30).
    Returns the rows (columns in the model's order), the target times, and the
    number of hours whose weather was missing (filled with NaN; LightGBM copes).
    """
    hs = np.arange(1, horizons + 1)
    target_times = t + pd.to_timedelta(hs, unit="h")
    w = weather.set_index("time_utc")[weather_cols]
    now = w.reindex([t]).iloc[0]
    tgt = w.reindex(target_times)
    missing = int(tgt.isna().all(axis=1).sum() + (1 if now.isna().all() else 0))

    # Feast returns None for a feature with no value (e.g. a lag hour that was not
    # collected); training had NaN there, so serving must too (LightGBM handles NaN).
    cols: dict[str, object] = {
        name: np.nan if v is None else float(v) for name, v in pm.items()
    }
    cols.update({f"{c}_now": float(now[c]) for c in weather_cols})
    cols["horizon"] = hs
    cols.update({f"{c}_target": tgt[c].to_numpy() for c in weather_cols})
    cal = calendar_features(pd.Series(target_times.tz_convert(None)), cal_cfg)
    cols.update({c: cal[c].to_numpy() for c in cal.columns})
    cols["city"] = city_code
    cols["region"] = region_code
    rows = pd.DataFrame(cols, index=range(horizons))
    # float64 throughout: missing weather must stay NaN, never an object column
    return rows[feature_order].astype("float64"), target_times, missing
