"""Unit tests for the data.gov.in collector helpers (Stage 1; exit codes Stage 10)."""

import json

import pandas as pd
import pytest

import airsense.ingestion.datagov_client as dg
from airsense.ingestion.datagov_client import _redact, save_snapshot


def test_api_key_is_removed_from_log_text():
    msg = "Connection reset: /resource/x?api-key=SECRET123&format=json"
    assert (
        _redact(msg, "SECRET123")
        == "Connection reset: /resource/x?api-key=***&format=json"
    )


def test_redact_with_empty_key_leaves_text_unchanged():
    assert _redact("no key here", "") == "no key here"


def test_snapshot_is_saved_in_a_dated_folder(tmp_path):
    path = save_snapshot([{"city": "Delhi", "avg_value": "190"}], out_dir=tmp_path)
    assert path.parent.parent == tmp_path  # <out>/<YYYY-MM-DD>/<HHMM>.csv
    saved = pd.read_csv(path)
    assert list(saved.columns) == ["city", "avg_value", "fetched_at_utc"]


@pytest.fixture
def collector(tmp_path, monkeypatch):
    """The collector with a fake feed, writing into tmp_path."""
    monkeypatch.setattr(dg, "STATUS_FILE", tmp_path / "logs" / "collector_status.json")
    monkeypatch.setattr(dg, "OUT_DIR", tmp_path / "datagov")
    monkeypatch.setenv("DATA_GOV_IN_API_KEY", "k")
    monkeypatch.setattr(dg, "load_dotenv", lambda *a, **k: None)
    return tmp_path


def test_empty_feed_is_a_source_gap_not_a_failure(collector, monkeypatch):
    monkeypatch.setattr(dg, "fetch_all_records", lambda rid, key: [])
    assert dg.main() == dg.EXIT_SOURCE_EMPTY == 99  # Airflow: skipped, not failed
    status = json.loads((collector / "logs" / "collector_status.json").read_text())
    assert status["result"] == "empty" and status["last_ok_utc"] is None


def test_status_keeps_the_last_good_time(collector, monkeypatch):
    monkeypatch.setattr(
        dg, "fetch_all_records", lambda rid, key: [{"city": "Delhi"}] * 300
    )
    assert dg.main() == 0
    ok = json.loads((collector / "logs" / "collector_status.json").read_text())
    monkeypatch.setattr(dg, "fetch_all_records", lambda rid, key: [])
    dg.main()
    st = json.loads((collector / "logs" / "collector_status.json").read_text())
    assert ok["result"] == "ok" and st["result"] == "empty"
    assert st["last_ok_utc"] == ok["time_utc"]


def test_network_failure_is_an_error(collector, monkeypatch):
    def boom(rid, key):
        raise RuntimeError("Giving up on offset 0")

    monkeypatch.setattr(dg, "fetch_all_records", boom)
    with pytest.raises(RuntimeError):
        dg.main()
    status = json.loads((collector / "logs" / "collector_status.json").read_text())
    assert status["result"] == "error"
