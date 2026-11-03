"""When should the monitor start a retrain? (Stage 10) Not a DAG itself.

Plain Python (standard library + PyYAML only), because it runs inside Airflow's
own environment, which does not have the AirSense package. Unit-tested in
tests/unit/test_monitor_decision.py.

A retrain is started when automatic retraining is on (monitoring.auto_retrain)
and either the forecast error is high (evaluation.json: error_high) or the
latest drift check, at most 26 hours old, found drift (drift.json: drift) -
but at most once every retrain_cooldown_days, so a lasting problem cannot start
one retrain after another. The quality gate inside airsense_retrain still
decides whether the new model replaces the champion.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path


def _read(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def decide(
    evaluation: dict | None,
    drift: dict | None,
    cfg: dict,
    last_trigger: dict | None,
    now: datetime,
) -> tuple[bool, str]:
    """(start a retrain?, reason in plain words)."""
    reasons = []
    if evaluation and evaluation.get("error_high"):
        o = evaluation["overall"]
        reasons.append(
            f"forecast error {o['mae_model']} µg/m³ is above the limit "
            f"{evaluation['limit_mae']} and worse than no change "
            f"({o['mae_no_change']})"
        )
    if drift and drift.get("drift"):
        age = now - datetime.fromisoformat(drift["checked_at_utc"])
        if age <= timedelta(hours=26):
            names = [n for n, v in drift["inputs"].items() if v.get("level") == "drift"]
            reasons.append("data drift in " + ", ".join(names))
    if not reasons:
        return False, "no reason to retrain: forecast error and drift are within limits"
    reason = "; ".join(reasons)
    if not cfg.get("auto_retrain"):
        return (
            False,
            f"{reason} - automatic retraining is off (monitoring.auto_retrain)",
        )
    if last_trigger:
        since = now - datetime.fromisoformat(last_trigger["triggered_at_utc"])
        if since < timedelta(days=cfg["retrain_cooldown_days"]):
            return False, (
                f"{reason} - but a retrain was already started "
                f"{since.days} day(s) ago (cooldown "
                f"{cfg['retrain_cooldown_days']} days)"
            )
    return True, reason


def decide_and_record(project: Path, cfg: dict, now: datetime | None = None) -> bool:
    """Read the reports, decide, and keep the decision for the console."""
    now = now or datetime.now(UTC)
    mon = project / "data" / "monitoring"
    trigger_file = mon / "retrain_trigger.json"
    start, reason = decide(
        _read(mon / "reports" / "evaluation.json"),
        _read(mon / "reports" / "drift.json"),
        cfg,
        _read(trigger_file),
        now,
    )
    decision = {"decided_at_utc": now.isoformat(), "retrain": start, "reason": reason}
    (mon / "reports").mkdir(parents=True, exist_ok=True)
    (mon / "reports" / "retrain_decision.json").write_text(
        json.dumps(decision, indent=2)
    )
    if start:
        trigger_file.write_text(
            json.dumps(
                {"triggered_at_utc": now.isoformat(), "reason": reason}, indent=2
            )
        )
    print(("Retrain: " if start else "No retrain: ") + reason)
    return start
