"""Sanity gate: a model may only be registered if it passes these checks (Stage 5).

All checks use the VALIDATION set, the set models are chosen on; the test set
stays an untouched final check. Thresholds: params.yaml -> gate.

  1. overall skill vs persistence >= min_skill;
  2. at every horizon, model MAE <= horizon_tolerance x persistence MAE;
  3. every city: skill >= min_city_skill (no city clearly worse than persistence);
  4. AQI-category accuracy >= min_aqi_accuracy;
  5. predictions finite and inside prediction_range; rows without a forecast
     (no current value in "change" mode) at most max_no_forecast_share.

Any failure -> exit code 1, so `dvc repro` stops before the `register` stage.

Input:  data/interim/train_metrics.json (written by train.py)
Output: data/interim/gate_report.json
"""

import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
METRICS_FILE = PROJECT_ROOT / "data" / "interim" / "train_metrics.json"
REPORT_FILE = PROJECT_ROOT / "data" / "interim" / "gate_report.json"


def check(name: str, passed: bool, value, threshold, detail: str = "") -> dict:
    return {
        "check": name,
        "passed": bool(passed),
        "value": value,
        "threshold": threshold,
        "detail": detail,
    }


def run_checks(m: dict, cfg: dict) -> list[dict]:
    """All gate checks on the training metrics; each returns passed / value / limit."""
    val = m["val"]["model"]
    results = [
        check(
            "overall_skill",
            val.get("skill", float("-inf")) >= cfg["min_skill"],
            round(val.get("skill", float("nan")), 4),
            cfg["min_skill"],
        )
    ]

    model_h, pers_h = m["val_by_horizon"]["model"], m["val_by_horizon"]["persistence"]
    bad_h = {
        h: (round(model_h[h]["mae"], 3), round(pers_h[h]["mae"], 3))
        for h in model_h
        if model_h[h]["mae"] > cfg["horizon_tolerance"] * pers_h[h]["mae"]
    }
    results.append(
        check(
            "every_horizon_vs_persistence",
            not bad_h,
            f"{len(bad_h)} of {len(model_h)} horizons fail",
            f"model MAE <= {cfg['horizon_tolerance']} x persistence MAE",
            "; ".join(
                f"h{h}: {a} vs {b}"
                for h, (a, b) in sorted(bad_h.items(), key=lambda kv: int(kv[0]))
            ),
        )
    )

    bad_c = {
        c: round(s.get("skill", float("nan")), 3)
        for c, s in m["val_by_city"].items()
        if s.get("skill", float("-inf")) < cfg["min_city_skill"]
    }
    results.append(
        check(
            "every_city_vs_persistence",
            not bad_c,
            f"{len(bad_c)} of {len(m['val_by_city'])} cities fail",
            f"city skill >= {cfg['min_city_skill']}",
            "; ".join(f"{c}: {v}" for c, v in sorted(bad_c.items())),
        )
    )

    results.append(
        check(
            "aqi_category_accuracy",
            val["aqi_accuracy"] >= cfg["min_aqi_accuracy"],
            round(val["aqi_accuracy"], 4),
            cfg["min_aqi_accuracy"],
        )
    )

    p = m["prediction_check"]["val"]
    lo, hi = cfg["prediction_range"]
    share = p.get("no_forecast_share", 0.0)
    results.append(
        check(
            "predictions_valid",
            p["non_finite"] == 0
            and p["min"] >= lo
            and p["max"] <= hi
            and share <= cfg["max_no_forecast_share"],
            f"min {p['min']:.1f}, max {p['max']:.1f}, non-finite {p['non_finite']}, "
            f"no forecast {share:.3%}",
            f"finite, within [{lo}, {hi}], no forecast <= "
            f"{cfg['max_no_forecast_share']:.1%}",
        )
    )
    return results


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    with open(PROJECT_ROOT / "params.yaml") as f:
        cfg = yaml.safe_load(f)["gate"]
    metrics = json.loads(METRICS_FILE.read_text())
    results = run_checks(metrics, cfg)
    passed = all(r["passed"] for r in results)

    REPORT_FILE.write_text(
        json.dumps(
            {
                "checked_at_utc": datetime.now(UTC).isoformat(),
                "mlflow_run_id": metrics["mlflow_run_id"],
                "passed": passed,
                "checks": results,
            },
            indent=2,
        )
    )
    for r in results:
        log.log(
            logging.INFO if r["passed"] else logging.ERROR,
            "%s %s: %s (limit: %s) %s",
            "PASS" if r["passed"] else "FAIL",
            r["check"],
            r["value"],
            r["threshold"],
            r["detail"],
        )
    if not passed:
        log.error("Gate FAILED: the model will not be registered")
        return 1
    log.info("Gate passed: run %s may be registered", metrics["mlflow_run_id"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
