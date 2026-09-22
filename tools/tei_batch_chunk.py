"""
Chunk OpenAI-compatible TEI /v1/embeddings requests to respect server max batch size.

TEI (text-embeddings-router) rejects requests when len(input) > max batch (often 32).
Set TEI_MAX_BATCH to match the container (default 32).
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Callable, List, Optional, Sequence

TEI_MAX_BATCH = max(1, int(os.environ.get("TEI_MAX_BATCH", "32")))


def tei_max_batch() -> int:
    return TEI_MAX_BATCH


def chunk_list(items: Sequence, size: Optional[int] = None) -> List[list]:
    n = size or TEI_MAX_BATCH
    return [list(items[i : i + n]) for i in range(0, len(items), n)]


def embed_documents_batched(
    texts: List[str],
    embed_fn: Callable[[List[str]], List[List[float]]],
    *,
    max_batch: Optional[int] = None,
) -> List[List[float]]:
    """Call embed_fn on slices of texts; concatenate vectors in order."""
    if not texts:
        return []
    limit = max_batch or TEI_MAX_BATCH
    if len(texts) <= limit:
        return embed_fn(texts)
    out: List[List[float]] = []
    for batch in chunk_list(texts, limit):
        out.extend(embed_fn(batch))
    return out


def urllib_post_embeddings(
    url: str,
    texts: List[str],
    *,
    model: str = "",
    timeout: float = 15.0,
    headers: Optional[dict] = None,
    max_batch: Optional[int] = None,
) -> List[List[float]]:
    """POST {"input": texts} to TEI with batch chunking. Raises on HTTP errors."""
    if not texts:
        return []
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    limit = max_batch or TEI_MAX_BATCH
    vectors: List[List[float]] = []
    for batch in chunk_list(texts, limit):
        payload = json.dumps({"input": batch, "model": model}).encode()
        req = urllib.request.Request(url, data=payload, headers=hdrs, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        ordered = sorted(data["data"], key=lambda x: x.get("index", 0))
        vectors.extend(item["embedding"] for item in ordered)
    return vectors
