"""Health-guidance texts: parse, embed, store in Qdrant, search (Stage 6, D53).

Source texts: guidance_docs/<category>.md, written in our own words from CPCB's AQI
health statements and WHO public advice (sources listed in each file). Each file
has a small header (category, PM2.5 range, sources) and one section per user group:
`## general`, `## sensitive`, `## outdoor_workers`. Every section becomes one point
in Qdrant: vector = sentence embedding of the text, payload = category, group,
text, sources.

Qdrant runs in local (embedded) mode on a folder now and as its own container in
Stage 8; only the client construction changes.

Run as the DVC stage `guidance_index`:  python src/airsense/guidance/index.py
"""

import json
import logging
import re
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import yaml

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
METRICS_FILE = PROJECT_ROOT / "data" / "interim" / "guidance_index.json"
GROUPS = {
    "general": "advice for the general public",
    "sensitive": "advice for children, older adults and people with asthma, "
    "heart or lung disease",
    "outdoor_workers": "advice for people who work or exercise outdoors",
}

Embedder = Callable[[list[str]], np.ndarray]


def parse_doc(text: str) -> list[dict]:
    """One guidance file -> one entry per group section."""
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, flags=re.DOTALL)
    if not m:
        raise ValueError("guidance file needs a --- header ---")
    meta, body = yaml.safe_load(m.group(1)), m.group(2)
    entries = []
    for group, section in re.findall(
        r"^## (\w+)\n(.*?)(?=^## |\Z)", body, re.MULTILINE | re.DOTALL
    ):
        if group not in GROUPS:
            raise ValueError(f"unknown group '{group}' in {meta.get('category')}")
        entries.append(
            {
                "category": meta["category"],
                "pm25_range": meta["pm25_range"],
                "group": group,
                "text": " ".join(section.split()),
                "sources": meta["sources"],
            }
        )
    return entries


def load_docs(docs_dir: Path) -> list[dict]:
    entries = []
    for path in sorted(docs_dir.glob("[0-9]*.md")):  # README.md is not guidance
        entries += parse_doc(path.read_text(encoding="utf-8"))
    return entries


def sentence_embedder(model_name: str) -> Embedder:
    """sentence-transformers model on CPU; vectors normalised for cosine search."""
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(model_name, device="cpu")
    return lambda texts: model.encode(texts, normalize_embeddings=True)


def open_client(path: Path):
    from qdrant_client import QdrantClient

    return QdrantClient(path=str(path))  # local mode; Stage 8: QdrantClient(url=...)


def build(client, collection: str, entries: list[dict], embed: Embedder) -> int:
    """(Re)create the collection and store one point per entry."""
    from qdrant_client.models import Distance, PointStruct, VectorParams

    texts = [f"{e['category']}. {GROUPS[e['group']]}. {e['text']}" for e in entries]
    vectors = np.asarray(embed(texts))
    if client.collection_exists(collection):
        client.delete_collection(collection)
    client.create_collection(
        collection,
        vectors_config=VectorParams(size=vectors.shape[1], distance=Distance.COSINE),
    )
    client.upsert(
        collection,
        points=[
            PointStruct(id=i, vector=v.tolist(), payload=e)
            for i, (v, e) in enumerate(zip(vectors, entries, strict=True))
        ],
    )
    return len(entries)


def search(
    client, collection: str, embed: Embedder, category: str, group: str, k: int = 1
):
    """Best guidance for an AQI category and a user group (filter + semantic match)."""
    from qdrant_client.models import FieldCondition, Filter, MatchValue

    query = np.asarray(embed([f"{category}. {GROUPS.get(group, group)}"]))[0]
    hits = client.query_points(
        collection,
        query=query.tolist(),
        query_filter=Filter(
            must=[FieldCondition(key="category", match=MatchValue(value=category))]
        ),
        limit=k,
    ).points
    return [{**h.payload, "score": round(float(h.score), 3)} for h in hits]


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    with open(PROJECT_ROOT / "params.yaml") as f:
        cfg = yaml.safe_load(f)["guidance"]
    start = time.perf_counter()
    entries = load_docs(PROJECT_ROOT / cfg["docs_dir"])
    embed = sentence_embedder(cfg["embedding_model"])
    client = open_client(PROJECT_ROOT / cfg["qdrant_path"])
    n = build(client, cfg["collection"], entries, embed)
    check = search(client, cfg["collection"], embed, "Poor", "sensitive")[0]
    client.close()
    elapsed = time.perf_counter() - start
    METRICS_FILE.write_text(
        json.dumps(
            {
                "built_at_utc": datetime.now(UTC).isoformat(),
                "points": n,
                "categories": sorted({e["category"] for e in entries}),
                "embedding_model": cfg["embedding_model"],
                "seconds": round(elapsed, 1),
                "check_query": {"category": "Poor", "group": "sensitive"},
                "check_result_group": check["group"],
            },
            indent=2,
        )
    )
    log.info(
        "Guidance index: %d texts in %.1f s; check -> %s", n, elapsed, check["group"]
    )
    if check["group"] != "sensitive":
        log.error("Check query returned the wrong group: %s", check["group"])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
