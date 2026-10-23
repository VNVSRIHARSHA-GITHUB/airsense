"""Register the trained model in the MLflow Model Registry (Stage 5, D50).

Champion / challenger:
  1. the run's model (it passed the gate) becomes a new version of the registered
     model, alias `challenger`; re-running for the same run reuses its version;
  2. if there is no champion yet, the new version becomes `champion`;
  3. otherwise the current champion is RE-SCORED on the challenger's validation
     rows (same data, fair comparison, also after retraining on new data) and the
     challenger takes `champion` only if its validation MAE is lower by at least
     registry.min_improvement (e.g. 1 %).

The serving API (Stage 6) loads `models:/<name>@champion`. MLflow's registry is
working state in mlflow.db; this script writes a record of each decision to
data/interim/registry.json, which is kept in Git (who was champion at each commit).
"""

import json
import logging
import os
import sys
import warnings
from datetime import UTC, datetime
from pathlib import Path

import yaml

from airsense.training.train import (
    TRAINING,
    encode_categories,
    load_training,
    score,
    split_masks,
    to_level,
)

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
METRICS_FILE = PROJECT_ROOT / "data" / "interim" / "train_metrics.json"
GATE_FILE = PROJECT_ROOT / "data" / "interim" / "gate_report.json"
RECORD_FILE = PROJECT_ROOT / "data" / "interim" / "registry.json"


def decide(challenger_mae: float, champion_mae: float | None, min_improvement: float):
    """Promote? Returns (promote, reason)."""
    if champion_mae is None:
        return True, "no champion yet"
    if champion_mae == float("inf"):
        return True, "champion could not be scored on the current data"
    limit = champion_mae * (1 - min_improvement)
    if challenger_mae <= limit:
        return True, (
            f"validation MAE {challenger_mae:.4f} <= {limit:.4f} "
            f"(champion {champion_mae:.4f} - {min_improvement:.0%})"
        )
    return False, (
        f"validation MAE {challenger_mae:.4f} > {limit:.4f} "
        f"(champion {champion_mae:.4f} - {min_improvement:.0%}): champion kept"
    )


def champion_val_mae(model_uri: str, target_mode: str, codes: dict, cfg: dict) -> float:
    """Score a registered model on the current validation rows (same as train.py)."""
    import mlflow

    model = mlflow.lightgbm.load_model(model_uri)
    features = model.feature_name()
    df = load_training(TRAINING)
    encode_categories(df, codes)
    val = df.loc[split_masks(df, cfg["train"]["split"])["val"]]
    del df
    missing = sorted(set(features) - set(val.columns))
    if missing:
        log.warning("Champion needs features not in the data: %s", missing)
        return float("inf")
    pred = to_level(model.predict(val[features]), val["pm25_24h"], target_mode)
    return score(val["target"], pred, val["pm25_24h"])["mae"]


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")
    warnings.filterwarnings("ignore", category=UserWarning)
    import mlflow
    from mlflow.exceptions import MlflowException
    from mlflow.tracking import MlflowClient

    with open(PROJECT_ROOT / "params.yaml") as f:
        params = yaml.safe_load(f)
    cfg = params["registry"]
    name = cfg["model_name"]
    metrics = json.loads(METRICS_FILE.read_text())
    gate = json.loads(GATE_FILE.read_text())
    run_id = metrics["mlflow_run_id"]
    if not gate["passed"] or gate["mlflow_run_id"] != run_id:
        log.error("The gate report does not approve run %s", run_id)
        return 1

    mlflow.set_tracking_uri(f"sqlite:///{PROJECT_ROOT / 'mlflow.db'}")
    client = MlflowClient()
    challenger_mae = metrics["val"]["model"]["mae"]

    # 1. register (or reuse the version already registered for this run)
    existing = client.search_model_versions(f"name='{name}' and run_id='{run_id}'")
    if existing:
        version = existing[0].version
        log.info("Run %s is already version %s of %s", run_id, version, name)
    else:
        version = mlflow.register_model(f"runs:/{run_id}/model", name).version
        log.info("Registered run %s as version %s of %s", run_id, version, name)
        run = client.get_run(run_id)
        for k, v in {
            "val_mae": f"{challenger_mae:.4f}",
            "test_mae": f"{metrics['test']['model']['mae']:.4f}",
            "target_mode": run.data.tags.get("target_mode", "level"),
            "training_data_md5": run.data.tags.get("training_data_md5", "unknown"),
            "git_commit": run.data.tags.get("git_commit", "unknown"),
            "gate": "passed",
        }.items():
            client.set_model_version_tag(name, version, k, v)
    client.set_registered_model_alias(name, "challenger", version)

    # 2-3. champion / challenger
    try:
        champ = client.get_model_version_by_alias(name, "champion")
    except MlflowException:
        champ = None
    if champ is not None and champ.version == version:
        promote, reason, champ_mae = False, "this version is already champion", None
    else:
        champ_mae = None
        if champ is not None:
            champ_mode = client.get_run(champ.run_id).data.tags.get(
                "target_mode", "level"
            )
            champ_mae = champion_val_mae(
                f"models:/{name}@champion",
                champ_mode,
                metrics["category_codes"],
                params,
            )
            log.info(
                "Champion v%s re-scored on validation: MAE %.4f",
                champ.version,
                champ_mae,
            )
        promote, reason = decide(challenger_mae, champ_mae, cfg["min_improvement"])
    if promote:
        client.set_registered_model_alias(name, "champion", version)
    champion = client.get_model_version_by_alias(name, "champion").version

    record = {
        "decided_at_utc": datetime.now(UTC).isoformat(),
        "model_name": name,
        "challenger_version": version,
        "challenger_run_id": run_id,
        "challenger_val_mae": round(challenger_mae, 4),
        "previous_champion_version": None if champ is None else champ.version,
        "previous_champion_val_mae": None if champ_mae is None else round(champ_mae, 4),
        "promoted": promote,
        "reason": reason,
        "champion_version": champion,
        "champion_uri": f"models:/{name}@champion",
    }
    RECORD_FILE.write_text(json.dumps(record, indent=2))
    log.info(
        "%s: v%s %s; champion is now v%s",
        "PROMOTED" if promote else "NOT promoted",
        version,
        reason,
        champion,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
