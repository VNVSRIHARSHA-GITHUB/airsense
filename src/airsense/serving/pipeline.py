"""Read-only pipeline status for the operators' console (Stage 7).

Everything comes from files the pipeline already writes; nothing is run or changed
except `dvc status`, which only compares checksums:
  - dvc.yaml            the stages, their inputs/outputs and the graph between them;
  - `dvc status --json` which stages are up to date and which inputs changed;
  - data/interim/*.json each stage's report: time, rows, seconds, checks, warnings;
  - gate_report.json, registry.json   the current model's checks and promotion;
  - data/raw/live/datagov/<date>/<HHMM>.csv   live collection health (names = UTC);
  - logs/*.log          recent warnings and errors;
  - logs/airflow_runs.json  the last run of each Airflow DAG (written by the DAGs).
The result is cached for `pipeline_cache_seconds`, so a console refresh does not
run `dvc status` again.
"""

import itertools
import json
import logging
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

from airsense.common.settings import PROJECT_ROOT

log = logging.getLogger(__name__)

SNAPSHOT_DIR = Path("data/raw/live/datagov")
LOG_DIR = Path("logs")
AIRFLOW_RUNS = LOG_DIR / "airflow_runs.json"
LOG_LINE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\S*\s+(WARNING|ERROR|CRITICAL)\s+(.*)$"
)
SKIP_KEYS = ("columns", "category_codes")  # long lists, not useful on the console


# ---------- DVC ----------


def _paths(entries) -> list[str]:
    """dvc.yaml entries are 'path' or {'path': {options}}."""
    out = []
    for e in entries or []:
        out += list(e) if isinstance(e, dict) else [e]
    return [str(Path(p)) for p in out]


def read_stages(dvc_yaml: Path) -> list[dict]:
    """Stages of dvc.yaml in file order, with their inputs, outputs and report."""
    spec = yaml.safe_load(dvc_yaml.read_text())["stages"]
    stages = []
    for name, s in spec.items():
        outs = _paths(s.get("outs")) + _paths(s.get("metrics"))
        reports = [
            p for p in outs if p.startswith("data/interim/") and p.endswith(".json")
        ]
        stages.append(
            {
                "name": name,
                "cmd": s.get("cmd", ""),
                "deps": _paths(s.get("deps")),
                "outs": outs,
                "report": reports[0] if reports else None,
                "always_changed": bool(s.get("always_changed", False)),
            }
        )
    return stages


def _inside(path: str, folder: str) -> bool:
    return path == folder or path.startswith(folder.rstrip("/") + "/")


def edges(stages: list[dict]) -> list[list[str]]:
    """A -> B when an output of A is (inside) an input of B, as `dvc dag` draws it."""
    result = []
    for a in stages:
        for b in stages:
            if a is b:
                continue
            if any(
                _inside(d, o) or _inside(o, d) for o in a["outs"] for d in b["deps"]
            ):
                result.append([a["name"], b["name"]])
    return result


def dvc_status(root: Path, timeout_s: float) -> dict:
    """`dvc status --json` -> {stage: [reasons]}; up-to-date stages are absent."""
    r = subprocess.run(
        [sys.executable, "-m", "dvc", "status", "--json"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip().splitlines()[-1][:300])
    return json.loads(r.stdout or "{}")


def stage_status(name: str, always_changed: bool, status: dict | None) -> tuple:
    """(status, reasons) for one stage from the parsed `dvc status`."""
    if status is None:
        return "unknown", []
    reasons = []
    for item in status.get(name, []):
        if isinstance(item, dict):
            for kind, files in item.items():  # e.g. "changed deps": {path: "modified"}
                reasons += [f"{kind}: {p} ({what})" for p, what in files.items()]
        elif item != "always changed":
            reasons.append(str(item))
    if reasons:
        return "changed", reasons
    return ("runs every time" if always_changed else "up to date"), []


# ---------- stage reports ----------


def _number(v) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


def summarise_report(report: dict) -> dict:
    """The useful top-level facts of a stage report, whatever its exact layout."""
    out = {}
    for k, v in report.items():
        if k in SKIP_KEYS:
            continue
        if (
            _number(v)
            or isinstance(v, str | bool)
            or v is None
            or (
                isinstance(v, list)
                and len(v) <= 20
                and all(not isinstance(x, list) for x in v)
            )
        ):
            out[k] = v
    return out


def report_time(report: dict) -> str | None:
    """The report's own timestamp (keys like built_at_utc, checked_at_utc)."""
    for k, v in report.items():
        if k.endswith("_at_utc") and isinstance(v, str):
            return v
    return None


def throughput(report: dict) -> dict | None:
    """Rows, seconds and rows/s if the report has them."""
    rate = next((v for k, v in report.items() if "per_s" in k and _number(v)), None)
    rows = next(  # "rows", or the first count named like rows_checked, raw_records
        (
            v
            for k, v in report.items()
            if _number(v) and ("rows" in k or "records" in k) and "per_s" not in k
        ),
        None,
    )
    if _number(report.get("rows")):
        rows = report["rows"]
    seconds = next(
        (v for k, v in report.items() if k.startswith("seconds") and _number(v)), None
    )
    if rate is None and rows and seconds:
        rate = rows / seconds
    if rate is None:
        return None
    return {"rows": rows, "seconds": seconds, "rows_per_second": round(float(rate))}


def read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


# ---------- live collection and logs ----------


def snapshot_times(folder: Path) -> list[datetime]:
    """UTC fetch times from <date>/<HHMM>.csv names."""
    times = []
    for f in folder.glob("*/*.csv"):
        try:
            times.append(
                datetime.strptime(f"{f.parent.name} {f.stem}", "%Y-%m-%d %H%M").replace(
                    tzinfo=UTC
                )
            )
        except ValueError:
            continue  # not a snapshot file
    return sorted(times)


def collection_health(
    times: list[datetime], now: datetime, every_min: int = 30
) -> dict:
    """Last snapshot, snapshots in the last 24 h, and gaps longer than 1.5 intervals."""
    day = [t for t in times if t > now - timedelta(hours=24)]
    gap_limit = timedelta(minutes=every_min * 1.5)
    gaps = [
        {
            "from_utc": a.isoformat(),
            "to_utc": b.isoformat(),
            "minutes": round((b - a).total_seconds() / 60),
        }
        for a, b in itertools.pairwise(day)
        if b - a > gap_limit
    ]
    last = times[-1] if times else None
    return {
        "last_snapshot_utc": last.isoformat() if last else None,
        "minutes_since_last": round((now - last).total_seconds() / 60)
        if last
        else None,
        "snapshots_24h": len(day),
        "expected_24h": 24 * 60 // every_min,
        "gaps_24h": gaps,
        "times_24h": [t.isoformat() for t in day],  # for the console's timeline
        "total_snapshots": len(times),
    }


def recent_problems(
    log_dir: Path, limit: int = 20, tail_lines: int = 3000
) -> list[dict]:
    """Newest WARNING/ERROR lines from logs/*.log (only the end of each file)."""
    found = []
    for f in sorted(log_dir.glob("*.log")):
        try:
            lines = f.read_text(errors="replace").splitlines()[-tail_lines:]
        except OSError:
            continue
        for line in lines:
            m = LOG_LINE.match(line)
            if m:
                found.append(
                    {
                        "time": m.group(1),
                        "level": m.group(2),
                        "file": f.name,
                        "message": m.group(3)[:300],
                    }
                )
    return sorted(found, key=lambda x: x["time"])[-limit:][::-1]


# ---------- scheduled workflows (Airflow) ----------


def workflows(runs: dict | None, now: datetime, airflow_url: str | None) -> list[dict]:
    """Last run of each Airflow DAG, newest first, with its age and a link."""
    out = []
    for dag_id, r in (runs or {}).items():
        end = r.get("end_date") or r.get("start_date")
        try:
            age = round((now - datetime.fromisoformat(end)).total_seconds() / 60)
        except (TypeError, ValueError):
            age = None
        out.append(
            {
                "dag_id": dag_id,
                "state": r.get("state"),
                "run_type": r.get("run_type"),
                "end_utc": end,
                "minutes_ago": age,
                "url": f"{airflow_url.rstrip('/')}/dags/{dag_id}"
                if airflow_url
                else None,
            }
        )
    return sorted(out, key=lambda w: w["dag_id"])


# ---------- the service ----------


class PipelineStatus:
    """Builds the console data; cached so refreshes are cheap."""

    def __init__(
        self,
        root: Path = PROJECT_ROOT,
        cache_seconds: float = 60,
        dvc_timeout_s: float = 60,
        status_fn: Callable[[Path, float], dict] = dvc_status,
        model_info: dict | None = None,
        airflow_url: str | None = None,
    ):
        self.root = root
        self.cache_seconds = cache_seconds
        self.dvc_timeout_s = dvc_timeout_s
        self.status_fn = status_fn
        self.model_info = model_info or {}
        self.airflow_url = airflow_url
        self._cache: dict | None = None
        self._at = -1e9
        self._lock = threading.Lock()

    def get(self) -> dict:
        with self._lock:
            if self._cache is None or time.monotonic() - self._at > self.cache_seconds:
                self._cache = self.build()
                self._at = time.monotonic()
            return self._cache

    def build(self) -> dict:
        now = datetime.now(UTC)
        stages = read_stages(self.root / "dvc.yaml")
        try:
            status, status_error = self.status_fn(self.root, self.dvc_timeout_s), None
        except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
            log.warning("dvc status failed: %s", exc)
            status, status_error = None, str(exc)

        rows, rates = [], []
        for s in stages:
            report = read_json(self.root / s["report"]) if s["report"] else None
            state, reasons = stage_status(s["name"], s["always_changed"], status)
            last_run = report_time(report) if report else None
            if last_run is None and s["outs"]:  # fall back to the output's file time
                mtimes = [
                    (self.root / o).stat().st_mtime
                    for o in s["outs"]
                    if (self.root / o).exists()
                ]
                if mtimes:
                    last_run = datetime.fromtimestamp(max(mtimes), UTC).isoformat()
            rate = throughput(report) if report else None
            if rate:
                rates.append({"stage": s["name"], **rate})
            rows.append(
                {
                    "name": s["name"],
                    "cmd": s["cmd"],
                    "status": state,
                    "reasons": reasons,
                    "last_run_utc": last_run,
                    "report_file": s["report"],
                    "report": summarise_report(report) if report else None,
                }
            )

        interim = self.root / "data" / "interim"
        return {
            "generated_at_utc": now.isoformat(),
            "dvc_status_error": status_error,
            "stages": rows,
            "edges": edges(stages),
            "throughput": rates,
            "gate": read_json(interim / "gate_report.json"),
            "registry": read_json(interim / "registry.json"),
            "champion": self.model_info,
            "collection": collection_health(
                snapshot_times(self.root / SNAPSHOT_DIR), now
            ),
            "recent_problems": recent_problems(self.root / LOG_DIR),
            "airflow_url": self.airflow_url,
            "workflows": workflows(
                read_json(self.root / AIRFLOW_RUNS), now, self.airflow_url
            ),
        }
