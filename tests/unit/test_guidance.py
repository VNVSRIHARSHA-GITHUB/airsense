"""Unit tests for the health-guidance index (Stage 6)."""

import hashlib
from pathlib import Path

import numpy as np
import pytest

from airsense.guidance.index import build, load_docs, open_client, parse_doc, search

DOCS = Path(__file__).resolve().parents[2] / "guidance_docs"


def fake_embed(texts):
    """Word-hash vectors: no model download needed in tests."""
    out = np.zeros((len(texts), 64))
    for i, t in enumerate(texts):
        for w in t.lower().replace(",", " ").replace(".", " ").split():
            out[i, int(hashlib.md5(w.encode()).hexdigest(), 16) % 64] += 1
    return out / np.linalg.norm(out, axis=1, keepdims=True)


def test_all_categories_and_groups_present():
    entries = load_docs(DOCS)
    cats = {e["category"] for e in entries}
    assert cats == {"Good", "Satisfactory", "Moderate", "Poor", "Very Poor", "Severe"}
    assert len(entries) == 18
    assert all(e["sources"] and e["text"] for e in entries)


def test_header_is_required():
    with pytest.raises(ValueError):
        parse_doc("# no header\n## general\ntext")


def test_search_filters_by_category_and_finds_the_group(tmp_path):
    client = open_client(tmp_path / "qdrant")
    assert build(client, "guidance", load_docs(DOCS), fake_embed) == 18
    hit = search(client, "guidance", fake_embed, "Poor", "sensitive")[0]
    assert hit["category"] == "Poor"
    assert hit["group"] == "sensitive"
    client.close()


def test_every_category_and_group_gets_its_own_text(tmp_path):
    """All 18 pairs, with an embedder that cannot tell the texts apart: only the
    group filter can pick the right one (with the group matched by meaning, the
    real model gave Very Poor + general -> the outdoor workers' text)."""

    def same_vector(texts):
        return np.ones((len(texts), 8)) / np.sqrt(8)

    client = open_client(tmp_path / "qdrant")
    entries = load_docs(DOCS)
    build(client, "guidance", entries, same_vector)
    for e in entries:
        hit = search(client, "guidance", same_vector, e["category"], e["group"])[0]
        assert (hit["category"], hit["group"]) == (e["category"], e["group"])
    client.close()
