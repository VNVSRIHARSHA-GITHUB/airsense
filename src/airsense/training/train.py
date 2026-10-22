"""Train, evaluate and explain the PM2.5 forecaster, tracked in MLflow (Stage 4).

1. Load the training table (data/training/, built through Feast in Stage 3).
2. Split by time (D44): train / validation / test, with a gap at each boundary so
   no target window of one set overlaps another.
3. Baselines: persistence (the current value for every horizon) and "same hour
   yesterday" (the value at t + h - 24, already known at t).
4. LightGBM: one model with the horizon as a feature (D31), early stopping on the
   validation set.
5. Metrics (D45): MAE, RMSE, skill vs persistence, AQI-category accuracy; overall,
   per horizon and per city.
6. SHAP: what drives the forecasts, and how that changes with the horizon.
7. MLflow (D46): parameters, metrics, the model with its signature, the plots, the
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
    operating system, otherwise loading needs twice the table's size (D47).
    """
    table = ds.dataset(str(path), format="parquet").to_table()
    df = table.to_pandas(strings_to_categorical=True, self_destruct=True)
    del table
    pa.default_memory_pool().release_unused()
    return df


def split_masks(df: pd.DataFrame, cfg: dict) -> dict[str, np.ndarray]:
    """Train / validation / test by time, with gaps (D44).

    train: target before train_end; validation: forecast time from val_start and
    target before val_end; test: forecast time from test_start. The day between
    each end and the next start is left out, so target windows never overlap.
    """
    t, tt = df["time_utc"], df["target_time_utc"]

    def ts(s: str) -> pd.Timestamp:
        return pd.Timestamp(s, tz="UTC")

    return {  # boolean masks, not copies (D47)
        "train": (tt < ts(cfg["train_end"])).to_numpy(),
        "val": ((t >= ts(cfg["val_start"])) & (tt < ts(cfg["val_end"]))).to_numpy(),
        "test": (t >= ts(cfg["test_start"])).to_numpy(),
    }


def category_codes(params: dict) -> dict[str, dict[str, int]]:
    """Fixed integer codes for city and region, taken from params.yaml.

    The model receives numbers, not text, so the same codes work in training, in
    the MLflow model (JSON input) and in the serving API (D46).
    """
    cities = params["data"]["cities"]
    regions = sorted({v["region"] for v in cities.values()})
    return {
        "city": {c: i for i, c in enumerate(cities)},
        "region": {r: i for i, r in enumerate(regions)},
    }


def encode_categories(df: pd.DataFrame, codes: dict) -> pd.DataFrame:
    """Replace city and region names by their fixed codes, IN PLACE (D47).

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
    """What the model learns (D48): the level itself, or the change from now.

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
    df: pd.DataFrame, features: list[str], mask: np.ndarray
) -> np.ndarray:
    """Selected rows as one float32 array, filled column by column (D47).

    Going through df.loc[mask, features].to_numpy() would hold two extra copies
    of the training rows at once; this holds one.
    """
    x = np.empty((int(mask.sum()), len(features)), dtype=np.float32, order="F")
    for j, c in enumerate(features):
        x[:, j] = df[c].to_numpy()[mask]
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


def training_data_md5() -> str:
    """Fingerprint of data/training as recorded by DVC (dvc.lock)."""
    try:
        lock = yaml.safe_load((PROJECT_ROOT / "dvc.lock").read_text())
        for out in lock["stages"]["build_training"]["outs"]:
            if out["path"] == "data/training":
                return out["md5"]
    except (OSError, KeyError, TypeError):
        pass
    return "unknown"


# ---------- plots ----------


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
    rows = {
        "train": int(train_mask.sum()),
        **{k: int(masks[k].sum()) for k in ("val", "test")},
    }
    log.info(
        "Rows: train %d, val %d, test %d; %d features",
        rows["train"],
        rows["val"],
        rows["test"],
        len(features),
    )

    # Memory (D47): keep the small validation/test slices, turn the training rows
    # into ONE float32 array, free the table, then let LightGBM bin the array.
    lgb_params = {**cfg["lgbm"]}
    categorical = list(codes)  # city, region
    parts = {k: df.loc[masks[k]].copy() for k in ("val", "test")}  # for evaluation
    mode = cfg["target_mode"]
    label = training_label(df["target"], df["pm25_24h"], mode)
    train_mask = train_mask & ~np.isnan(label)  # "change" needs a current value
    x = feature_matrix(df, features, train_mask)
    y = label[train_mask]
    del df, label
    train_set = lgb.Dataset(
        x,
        label=y,
        feature_name=features,
        categorical_feature=categorical,
        free_raw_data=True,
    ).construct()
    del x, y
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

    results, per_h, per_city = {}, {}, {}
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
        if name == "test":
            per_h = {m: score_groups(part, m, "horizon") for m in results[name]}
            names = {v: k for k, v in codes["city"].items()}
            per_city = score_groups(
                part.assign(city=part["city"].map(names)), "model", "city"
            )
            test_part = part
    elapsed = time.perf_counter() - start

    MODEL_FILE.parent.mkdir(parents=True, exist_ok=True)
    booster.save_model(str(MODEL_FILE), num_iteration=booster.best_iteration)
    REPORTS.mkdir(parents=True, exist_ok=True)
    plot_by_horizon(per_h, REPORTS / "mae_by_horizon.png")
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
                "model_type": "LightGBM, one model with horizon as a feature (D31)",
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
        for h in sorted(int(h) for h in per_h["model"]):
            for model_name in per_h:
                if "mae" in per_h[model_name].get(str(h), {}):
                    mlflow.log_metric(
                        f"test_mae_by_horizon_{model_name}",
                        per_h[model_name][str(h)]["mae"],
                        step=h,
                    )
        for city, s in per_city.items():
            mlflow.log_metric(f"test_mae_city_{city.replace(' ', '_')}", s["mae"])
        mlflow.log_metric("train_seconds", train_s)
        mlflow.log_artifacts(str(REPORTS), artifact_path="plots")
        mlflow.log_dict(codes, "category_codes.json")  # needed to call the model
        x_example = sample.head(5)
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
        "test_by_city": per_city,
        "test_mae_by_horizon": {
            m: {h: s.get("mae") for h, s in v.items()} for m, v in per_h.items()
        },
        "shap_mean_abs": importance,
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
