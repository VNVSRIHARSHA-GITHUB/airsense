"""Train, evaluate and explain the PM2.5 forecaster, tracked in MLflow (Stage 4).

1. Load the training table (data/training/, built through Feast in Stage 3).
2. Split by time: train / validation / test, with a gap at each boundary so
   no target window of one set overlaps another.
2b. Live rows (data/training_live/, Stage 10): the hours collected since Oct 2026.
   The newest `live_holdout_days` are kept aside ("recent live", reported only);
   the rest join the TRAINING rows with a higher weight, so together they carry
   `live_weight_share` of the total weight. They never enter validation or test,
   so the gate keeps comparing models on the same historical periods.
3. Baselines: persistence (the current value for every horizon) and "same hour
   yesterday" (the value at t + h - 24, already known at t).
4. LightGBM: one model with the horizon as a feature, early stopping on the
   validation set.
5. Metrics: MAE, RMSE, skill vs persistence, AQI-category accuracy; overall,
   per horizon and per city.
6. SHAP: what drives the forecasts, and how that changes with the horizon.
7. MLflow: parameters, metrics, the model with its signature, the plots, the
   Git commit and the fingerprint of the training data.

Outputs: models/lgbm_model.txt, reports/train/*.png,
         data/interim/train_metrics.json; MLflow run in mlflow.db / mlruns/.
"""

import json
import logging
import os
import subprocess
import sys
import time
import warnings
from datetime import UTC, datetime
from pathlib import Path

import lightgbm as lgb
import matplotlib
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.dataset as ds
import yaml

matplotlib.use("Agg")  # write image files, no window
import matplotlib.pyplot as plt

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
TRAINING = PROJECT_ROOT / "data" / "training"
TRAINING_LIVE = PROJECT_ROOT / "data" / "training_live"
MODEL_FILE = PROJECT_ROOT / "models" / "lgbm_model.txt"
REPORTS = PROJECT_ROOT / "reports" / "train"
METRICS_FILE = PROJECT_ROOT / "data" / "interim" / "train_metrics.json"

NOT_FEATURES = ["time_utc", "target_time_utc", "target"]
# CPCB AQI bands for 24-hour PM2.5 (ug/m3): upper edges of each category.
AQI_EDGES = [30, 60, 90, 120, 250]
AQI_NAMES = ["Good", "Satisfactory", "Moderate", "Poor", "Very Poor", "Severe"]


# ---------- data ----------


def load_training(path: Path) -> pd.DataFrame:
    """Read all horizon files; text columns become categories (saves ~2 GB).

    Arrow keeps freed buffers for reuse; release_unused() hands them back to the
    operating system, otherwise loading needs twice the table's size.
    """
    table = ds.dataset(str(path), format="parquet").to_table()
    df = table.to_pandas(strings_to_categorical=True, self_destruct=True)
    del table
    pa.default_memory_pool().release_unused()
    return df


def split_masks(df: pd.DataFrame, cfg: dict) -> dict[str, np.ndarray]:
    """Train / validation / test by time, with gaps.

    train: target before train_end; validation: forecast time from val_start and
    target before val_end; test: forecast time from test_start. The day between
    each end and the next start is left out, so target windows never overlap.
    """
    t, tt = df["time_utc"], df["target_time_utc"]

    def ts(s: str) -> pd.Timestamp:
        return pd.Timestamp(s, tz="UTC")

    return {  # boolean masks, not copies (saves memory)
        "train": (tt < ts(cfg["train_end"])).to_numpy(),
        "val": ((t >= ts(cfg["val_start"])) & (tt < ts(cfg["val_end"]))).to_numpy(),
        "test": (t >= ts(cfg["test_start"])).to_numpy(),
    }


def load_live(path: Path, columns: list[str]) -> pd.DataFrame | None:
    """The live training rows with the history table's columns, or None if none."""
    files = sorted(path.glob("h*.parquet")) if path.exists() else []
    if not files:
        return None
    live = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    if live.empty:
        return None
    missing = sorted(set(columns) - set(live.columns))
    if missing:
        raise ValueError(f"Live training rows lack columns: {missing}")
    return live[columns]


def live_split(
    live: pd.DataFrame, holdout_days: float
) -> tuple[np.ndarray, np.ndarray]:
    """(train, recent) masks for the live rows.

    recent: forecast time after the cut (newest target minus holdout_days), so all
    its targets are after the cut too; train: target at or before the cut. Rows
    whose forecast is before the cut but target after it are left out, like the
    gaps between the historical splits.
    """
    cut = live["target_time_utc"].max() - pd.Timedelta(holdout_days, unit="D")
    return (
        (live["target_time_utc"] <= cut).to_numpy(),
        (live["time_utc"] > cut).to_numpy(),
    )


def live_weight(n_history: int, n_live: int, share: float, max_weight: float) -> float:
    """Weight of one live row so live rows carry `share` of the total weight.

    share = n_live * w / (n_history + n_live * w)  =>  w = share/(1-share) * n_history/n_live.
    Capped at max_weight: with only a few days of live data, a huge weight would
    make the model memorise those days.
    """
    if n_live == 0 or share <= 0:
        return 1.0
    return float(min(share / (1 - share) * n_history / n_live, max_weight))


def category_codes(params: dict) -> dict[str, dict[str, int]]:
    """Fixed integer codes for city and region, taken from params.yaml.

    The model receives numbers, not text, so the same codes work in training, in
    the MLflow model (JSON input) and in the serving API.
    """
    cities = params["data"]["cities"]
    regions = sorted({v["region"] for v in cities.values()})
    return {
        "city": {c: i for i, c in enumerate(cities)},
        "region": {r: i for i, r in enumerate(regions)},
    }


def encode_categories(df: pd.DataFrame, codes: dict) -> pd.DataFrame:
    """Replace city and region names by their fixed codes, IN PLACE (saves memory).

    The mapping is applied to the few distinct names, then spread to all rows by
    position: no per-row text and no copy of the 14 M-row table. Unknown or
    missing names raise an error.
    """
    for col, mapping in codes.items():
        cat = df[col].astype("category")
        lookup = cat.cat.categories.astype(str).map(mapping)
        unknown = sorted(cat.cat.categories[lookup.isna()].astype(str))
        positions = cat.cat.codes.to_numpy()
        if unknown or (positions < 0).any():
            raise ValueError(f"Unknown or missing {col} values: {unknown}")
        df[col] = np.asarray(lookup, dtype=np.int16)[positions]
    return df


def training_label(target, current, mode: str) -> np.ndarray:
    """What the model learns: the level itself, or the change from now.

    "change" = target - current pm25_24h. At short horizons the future 24-h
    average is mostly the current one, so learning the change lets the model
    start from persistence instead of rebuilding the level from scratch.
    """
    target = np.asarray(target, dtype=np.float32)
    if mode == "level":
        return target
    if mode == "change":
        return target - np.asarray(current, dtype=np.float32)
    raise ValueError(f"unknown target_mode: {mode}")


def to_level(pred, current, mode: str) -> np.ndarray:
    """Turn model output back into a PM2.5 forecast (inverse of training_label)."""
    pred = np.asarray(pred, dtype=np.float64)
    return pred if mode == "level" else pred + np.asarray(current, dtype=np.float64)


def feature_matrix(
    df: pd.DataFrame,
    features: list[str],
    mask: np.ndarray,
    extra: pd.DataFrame | None = None,
) -> np.ndarray:
    """Selected rows (plus all rows of `extra`) as one float32 array, filled column
    by column.

    Going through df.loc[mask, features].to_numpy() would hold two extra copies
    of the training rows at once, and np.concatenate with the live rows another;
    this holds one.
    """
    n = int(mask.sum())
    m = 0 if extra is None else len(extra)
    x = np.empty((n + m, len(features)), dtype=np.float32, order="F")
    for j, c in enumerate(features):
        x[:n, j] = df[c].to_numpy()[mask]
        if m:
            x[n:, j] = extra[c].to_numpy()
    return x


def feature_columns(df: pd.DataFrame) -> list[str]:
    return [c for c in df.columns if c not in NOT_FEATURES]


def yesterday_baseline(df: pd.DataFrame) -> pd.Series:
    """pm25_24h at t + h - 24 for each row: known at forecast time t (h <= 24)."""
    known = df.loc[df["horizon"] == 1, ["city", "time_utc", "pm25_24h"]]
    lookup = known.rename(columns={"time_utc": "when", "pm25_24h": "yesterday"})
    keys = pd.DataFrame(
        {
            "city": df["city"].to_numpy(),
            "when": df["target_time_utc"] - pd.to_timedelta(24, unit="h"),
        }
    )
    out = keys.merge(lookup, on=["city", "when"], how="left")["yesterday"]
    return pd.Series(out.to_numpy(dtype=np.float32), index=df.index)


# ---------- metrics ----------


def aqi_category(values) -> np.ndarray:
    """CPCB AQI category index (0 = Good ... 5 = Severe); -1 where missing."""
    v = np.asarray(values, dtype=float)
    cat = np.searchsorted(AQI_EDGES, v, side="left")  # 30 -> Good, 30.1 -> Satisf.
    return np.where(np.isnan(v), -1, cat)


def score(y, pred, persistence) -> dict:
    """MAE, RMSE, skill vs persistence and AQI-category accuracy on valid rows."""
    y, pred, pers = (np.asarray(a, dtype=float) for a in (y, pred, persistence))
    ok = ~np.isnan(y) & ~np.isnan(pred)
    out = {"n": int(ok.sum())}
    if not ok.any():
        return out
    err = pred[ok] - y[ok]
    out["mae"] = float(np.mean(np.abs(err)))
    out["rmse"] = float(np.sqrt(np.mean(err**2)))
    out["aqi_accuracy"] = float(np.mean(aqi_category(pred[ok]) == aqi_category(y[ok])))
    both = ok & ~np.isnan(pers)
    if both.any():
        mae_p = np.mean(np.abs(pers[both] - y[both]))
        out["skill"] = float(1 - np.mean(np.abs(pred[both] - y[both])) / mae_p)
    return out


def score_groups(df: pd.DataFrame, pred_col: str, by: str) -> dict:
    """score() per value of `by` (horizon or city)."""
    return {
        str(k): score(g["target"], g[pred_col], g["persistence"])
        for k, g in df.groupby(by, observed=True)
    }


# ---------- tracking helpers ----------


def git_info() -> dict:
    """Commit and whether tracked files had uncommitted changes."""
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        return {"git_commit": commit, "git_dirty": str(bool(dirty))}
    except (OSError, subprocess.CalledProcessError):
        return {"git_commit": "unknown", "git_dirty": "unknown"}


def training_data_md5(
    stage: str = "build_training", path: str = "data/training"
) -> str:
    """Fingerprint of a training-data folder as recorded by DVC (dvc.lock)."""
    try:
        lock = yaml.safe_load((PROJECT_ROOT / "dvc.lock").read_text())
        for out in lock["stages"][stage]["outs"]:
            if out["path"] == path:
                return out["md5"]
    except (OSError, KeyError, TypeError):
        pass
    return "unknown"


# ---------- plots ----------


def signature_example(sample: pd.DataFrame) -> pd.DataFrame:
    """Rows for the MLflow signature, weather columns as float64 ("double").

    In the table, humidity and cloud cover are whole numbers (int16: Open-Meteo
    reports them so) and the other weather columns float32. A signature declaring
    "integer" makes the model server reject an hour with one empty value (a missing
    value is not an integer), and the forecast fails. Weather is a measurement, so
    "double" for all of it: empty values are then allowed, like for every other input.
    """
    out = sample.copy()
    for c in out.columns:
        if c.endswith(("_now", "_target")):
            out[c] = out[c].astype(np.float64)
    return out


def plot_by_horizon(per_h: dict[str, dict], path: Path) -> None:
    hs = sorted(int(h) for h in per_h["model"])
    plt.figure(figsize=(7, 4))
    for name, label in [
        ("model", "LightGBM"),
        ("persistence", "Persistence"),
        ("yesterday", "Same hour yesterday"),
    ]:
        plt.plot(hs, [per_h[name][str(h)].get("mae", np.nan) for h in hs], label=label)
    plt.xlabel("Horizon (hours ahead)")
    plt.ylabel("MAE (ug/m3), test set")
    plt.legend()
    plt.grid(alpha=0.3)
    plt.tight_layout()
    plt.savefig(path, dpi=120)
    plt.close()


def shap_plots(booster, x: pd.DataFrame, out_dir: Path) -> dict:
    """SHAP values via LightGBM's built-in TreeSHAP; returns mean |SHAP| per feature."""
    import shap

    contrib = booster.predict(x, pred_contrib=True)[:, :-1]  # last column = bias
    names = list(x.columns)
    shap.summary_plot(contrib, x, feature_names=names, show=False, max_display=20)
    plt.tight_layout()
    plt.savefig(out_dir / "shap_summary.png", dpi=120)
    plt.close()

    importance = pd.Series(np.abs(contrib).mean(axis=0), index=names)
    short = np.abs(contrib[x["horizon"].to_numpy() <= 6]).mean(axis=0)
    long_ = np.abs(contrib[x["horizon"].to_numpy() >= 18]).mean(axis=0)
    top = importance.sort_values(ascending=False).index[:12]
    idx = [names.index(c) for c in top]
    y = np.arange(len(top))
    plt.figure(figsize=(7, 5))
    plt.barh(y - 0.2, short[idx], height=0.4, label="h = 1-6")
    plt.barh(y + 0.2, long_[idx], height=0.4, label="h = 18-24")
    plt.yticks(y, top)
    plt.gca().invert_yaxis()
    plt.xlabel("mean |SHAP| (ug/m3)")
    plt.legend()
    plt.tight_layout()
    plt.savefig(out_dir / "shap_by_horizon.png", dpi=120)
    plt.close()
    return {
        k: round(float(v), 3)
        for k, v in importance.sort_values(ascending=False).items()
    }


# ---------- main ----------


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")
    warnings.filterwarnings("ignore", category=UserWarning)
    import mlflow
    from mlflow.models import infer_signature

    with open(PROJECT_ROOT / "params.yaml") as f:
        params = yaml.safe_load(f)
    cfg = params["train"]

    start = time.perf_counter()
    df = load_training(TRAINING)
    codes = category_codes(params)
    encode_categories(df, codes)  # in place
    features = feature_columns(df)
    df["persistence"] = df["pm25_24h"]  # the persistence forecast (float32, ~60 MB)
    df["yesterday"] = yesterday_baseline(df)
    masks = split_masks(df, cfg["split"])
    train_mask = masks["train"]
    if cfg["train_fraction"] < 1:  # optional subsample, to save memory and time
        rng = np.random.default_rng(cfg["lgbm"]["seed"])
        train_mask = train_mask & (rng.random(len(df)) < cfg["train_fraction"])

    # Live rows: kept apart from df, so they can never fall into val or test
    # (the test mask "from 2022-10-02" would otherwise include all of 2026).
    mode = cfg["target_mode"]
    live = load_live(
        TRAINING_LIVE, [c for c in df.columns if c not in ("persistence", "yesterday")]
    )
    live_train, recent = None, None
    if live is not None:
        encode_categories(live, codes)
        live_mask, recent_mask = live_split(live, cfg["live_holdout_days"])
        recent = live.loc[recent_mask].reset_index(drop=True)
        live_train = live.loc[live_mask].reset_index(drop=True)
        live_label = training_label(live_train["target"], live_train["pm25_24h"], mode)
        live_train = live_train.loc[~np.isnan(live_label)].reset_index(drop=True)
        del live
    n_live = 0 if live_train is None else len(live_train)

    rows = {
        "train": int(train_mask.sum()),
        **{k: int(masks[k].sum()) for k in ("val", "test")},
        "live_train": n_live,
        "live_recent": 0 if recent is None else len(recent),
    }
    log.info(
        "Rows: train %d (+%d live), val %d, test %d, recent live %d; %d features",
        rows["train"],
        rows["live_train"],
        rows["val"],
        rows["test"],
        rows["live_recent"],
        len(features),
    )

    # Memory: keep the small validation/test slices, turn the training rows
    # into ONE float32 array, free the table, then let LightGBM bin the array.
    lgb_params = {**cfg["lgbm"]}
    categorical = list(codes)  # city, region
    parts = {k: df.loc[masks[k]].copy() for k in ("val", "test")}  # for evaluation
    label = training_label(df["target"], df["pm25_24h"], mode)
    train_mask = train_mask & ~np.isnan(label)  # "change" needs a current value
    n_hist = int(train_mask.sum())
    w_live = live_weight(
        n_hist, n_live, cfg["live_weight_share"], cfg["live_weight_max"]
    )
    x = feature_matrix(df, features, train_mask, extra=live_train)
    y = label[train_mask]
    if n_live:
        y = np.concatenate(
            [y, training_label(live_train["target"], live_train["pm25_24h"], mode)]
        )
    weight = None
    if n_live:
        weight = np.ones(len(y), dtype=np.float32)
        weight[n_hist:] = w_live
    live_info = {
        "rows_train": n_live,
        "rows_recent": rows["live_recent"],
        "weight": round(w_live, 3) if n_live else None,
        "weight_share": round(n_live * w_live / (n_hist + n_live * w_live), 4)
        if n_live
        else 0.0,
        "holdout_days": cfg["live_holdout_days"],
        "first_target_utc": str(live_train["target_time_utc"].min())
        if n_live
        else None,
        "last_target_utc": str(live_train["target_time_utc"].max()) if n_live else None,
    }
    del df, label, live_train
    train_set = lgb.Dataset(
        x,
        label=y,
        weight=weight,
        feature_name=features,
        categorical_feature=categorical,
        free_raw_data=True,
    ).construct()
    del x, y, weight
    if n_live:
        log.info(
            "Live rows: %d with weight %.2f (%.1f%% of the total weight)",
            n_live,
            w_live,
            100 * live_info["weight_share"],
        )
    val_set = lgb.Dataset(
        feature_matrix(parts["val"], features, np.ones(len(parts["val"]), bool)),
        label=training_label(parts["val"]["target"], parts["val"]["pm25_24h"], mode),
        feature_name=features,
        categorical_feature=categorical,
        reference=train_set,
    ).construct()
    t0 = time.perf_counter()
    booster = lgb.train(
        lgb_params,
        train_set,
        num_boost_round=cfg["num_boost_round"],
        valid_sets=[val_set],
        valid_names=["val"],
        callbacks=[
            lgb.early_stopping(cfg["early_stopping_rounds"], verbose=False),
            lgb.log_evaluation(100),
        ],
    )
    train_s = time.perf_counter() - t0
    log.info(
        "Trained %d trees in %.0f s (best iteration %d)",
        booster.num_trees(),
        train_s,
        booster.best_iteration,
    )

    results, per_h, per_city, checks = {}, {}, {}, {}
    names = {v: k for k, v in codes["city"].items()}
    for name in ("val", "test"):
        part = parts[name]
        part["model"] = to_level(
            booster.predict(part[features], num_iteration=booster.best_iteration),
            part["pm25_24h"],
            mode,
        )
        results[name] = {
            m: score(part["target"], part[m], part["persistence"])
            for m in ("model", "persistence", "yesterday")
        }
        # Breakdowns for both sets: the gate (Stage 5) decides on validation.
        per_h[name] = {m: score_groups(part, m, "horizon") for m in results[name]}
        per_city[name] = score_groups(
            part.assign(city=part["city"].map(names)), "model", "city"
        )
        # In "change" mode a forecast needs the current value: hours without one
        # (station outages) get no forecast. Counted separately; every other
        # forecast must be a finite number (checked by the gate).
        pred = part["model"].to_numpy()
        expected = (
            part["pm25_24h"].notna().to_numpy()
            if mode == "change"
            else np.ones(len(part), bool)
        )
        checks[name] = {
            "non_finite": int((~np.isfinite(pred[expected])).sum()),
            "no_forecast_share": round(float((~expected).mean()), 5),
            "min": float(np.nanmin(pred)),
            "max": float(np.nanmax(pred)),
        }
    test_part = parts["test"]

    # Recent live data: the newest days, never trained on. Reported, not gated:
    # a few days of one season are too few to decide on (the gate uses validation).
    recent_live = None
    if recent is not None and len(recent):
        recent["model"] = to_level(
            booster.predict(recent[features], num_iteration=booster.best_iteration),
            recent["pm25_24h"],
            mode,
        )
        s_model = score(recent["target"], recent["model"], recent["pm25_24h"])
        if s_model["n"]:
            recent_live = {
                "from_utc": str(recent["time_utc"].min()),
                "to_utc": str(recent["target_time_utc"].max()),
                "model": s_model,
                "persistence": score(
                    recent["target"], recent["pm25_24h"], recent["pm25_24h"]
                ),
            }
            log.info(
                "Recent live data (%d rows): MAE model %.2f, no change %.2f",
                s_model["n"],
                s_model["mae"],
                recent_live["persistence"].get("mae", float("nan")),
            )
    elapsed = time.perf_counter() - start

    MODEL_FILE.parent.mkdir(parents=True, exist_ok=True)
    booster.save_model(str(MODEL_FILE), num_iteration=booster.best_iteration)
    REPORTS.mkdir(parents=True, exist_ok=True)
    plot_by_horizon(per_h["test"], REPORTS / "mae_by_horizon.png")
    sample = test_part[features].sample(
        min(cfg["shap_sample"], len(test_part)), random_state=0
    )
    importance = shap_plots(booster, sample, REPORTS)

    # ---- MLflow ----
    mlflow.set_tracking_uri(f"sqlite:///{PROJECT_ROOT / 'mlflow.db'}")
    exp_name = cfg["mlflow_experiment"]
    if mlflow.get_experiment_by_name(exp_name) is None:
        mlflow.create_experiment(
            exp_name, artifact_location=(PROJECT_ROOT / "mlruns").as_uri()
        )
    mlflow.set_experiment(exp_name)
    with mlflow.start_run(run_name=f"lgbm-{datetime.now(UTC):%Y%m%d-%H%M}") as run:
        mlflow.set_tags(
            {
                **git_info(),
                "training_data_md5": training_data_md5(),
                "training_live_md5": training_data_md5(
                    "build_training_live", "data/training_live"
                ),
                "model_type": "LightGBM, one model with horizon as a feature",
                "target_mode": mode,  # "change": add pm25_24h to the output
            }
        )
        mlflow.log_params(
            {
                **{f"lgbm.{k}": v for k, v in lgb_params.items()},
                **{f"split.{k}": v for k, v in cfg["split"].items()},
                "num_boost_round": cfg["num_boost_round"],
                "early_stopping_rounds": cfg["early_stopping_rounds"],
                "best_iteration": booster.best_iteration,
                "n_features": len(features),
                "rows_train": rows["train"],
                "rows_val": rows["val"],
                "rows_test": rows["test"],
                "rows_live_train": rows["live_train"],
                "live_weight": live_info["weight"],
                "live_weight_share": live_info["weight_share"],
                "live_holdout_days": cfg["live_holdout_days"],
                "train_fraction": cfg["train_fraction"],
                "target_mode": mode,
                "train_start": params["features"]["train_start"],
                "lags_hours": str(params["features"]["lags_hours"]),
            }
        )
        for split_name, res in results.items():
            for model_name, s in res.items():
                for k, v in s.items():
                    if k != "n":
                        mlflow.log_metric(f"{split_name}_{model_name}_{k}", v)
        for split_name, by_model in per_h.items():
            for model_name, by_h in by_model.items():
                for h, s in by_h.items():
                    if "mae" in s:
                        mlflow.log_metric(
                            f"{split_name}_mae_by_horizon_{model_name}",
                            s["mae"],
                            step=int(h),
                        )
        for split_name, by_city in per_city.items():
            for city, s in by_city.items():
                mlflow.log_metric(
                    f"{split_name}_mae_city_{city.replace(' ', '_')}", s["mae"]
                )
        mlflow.log_metric("train_seconds", train_s)
        if recent_live:
            for model_name in ("model", "persistence"):
                for k, v in recent_live[model_name].items():
                    if k != "n":
                        mlflow.log_metric(f"recent_live_{model_name}_{k}", v)
        mlflow.log_artifacts(str(REPORTS), artifact_path="plots")
        mlflow.log_dict(codes, "category_codes.json")  # needed to call the model
        x_example = signature_example(sample.head(5))
        mlflow.lightgbm.log_model(
            booster,
            name="model",
            signature=infer_signature(x_example, booster.predict(x_example)),
            input_example=x_example,
        )
        run_id = run.info.run_id

    summary = {
        "trained_at_utc": datetime.now(UTC).isoformat(),
        "mlflow_run_id": run_id,
        "best_iteration": booster.best_iteration,
        "train_seconds": round(train_s, 1),
        "seconds": round(elapsed, 1),
        "rows": rows,
        "n_features": len(features),
        "category_codes": codes,
        "val": results["val"],
        "test": results["test"],
        "prediction_check": checks,
        "val_by_horizon": per_h["val"],
        "val_by_city": per_city["val"],
        "test_by_city": per_city["test"],
        "test_mae_by_horizon": {
            m: {h: s.get("mae") for h, s in v.items()} for m, v in per_h["test"].items()
        },
        "shap_mean_abs": importance,
        "live_training": live_info,
        "recent_live": recent_live,
    }
    METRICS_FILE.parent.mkdir(parents=True, exist_ok=True)
    METRICS_FILE.write_text(json.dumps(summary, indent=2))
    t = results["test"]
    log.info(
        "Test MAE: LightGBM %.2f | persistence %.2f | yesterday %.2f | skill %.3f | "
        "AQI accuracy %.3f (MLflow run %s)",
        t["model"]["mae"],
        t["persistence"]["mae"],
        t["yesterday"]["mae"],
        t["model"].get("skill", float("nan")),
        t["model"]["aqi_accuracy"],
        run_id,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
