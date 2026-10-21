"""Unit tests for the data.gov.in collector helpers (Stage 1)."""

import pandas as pd

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
