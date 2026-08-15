#!/usr/bin/env python3
"""memory_search_shim.py — in-sandbox stand-in for the NATIVE memory_search tool.

WHY THIS EXISTS
  hybrid_recall fuses FOUR fuzzy legs: docs_search, session_search, graph_search,
  and memory_external. The first three are CLI tools it subprocesses itself. The
  fourth (memory_external) is NOT a CLI — in production it is the AGENT's native
  memory_search tool-call, whose hits the agent folds in via --external-hits.

  In the benchmark sandbox there is no agent runtime to make that native call, so
  the memory_external leg is DARK by construction: hybrid_recall runs as 3 legs,
  not 4, and every result mislabels itself "hybrid" while the native-memory leg
  contributes nothing. That understates neuron and misrepresents the fusion.

  This shim closes that gap FAITHFULLY: it does exactly what native memory_search
  does — semantic search over the workspace's distilled memory/*.md — using the
  SAME embedding model + e5 prefixing the other legs use (intfloat/multilingual-
  e5-small, query:/passage:, normalized, cosine). Its stdout is the exact
  --external-hits shape hybrid_recall expects: a JSON list of {content, score, uri}.

  It is a SANDBOX FIDELITY tool, not a memory_search reimplementation for prod
  (prod has the real native tool). It deliberately mirrors the native semantics
  (fuzzy semantic recall over memory/*.md) rather than inventing new behavior, so
  the benchmarked hybrid == the production hybrid.

USAGE
  python3 memory_search_shim.py "<query>" --memory-dir <lab>/memory [--k 5] [--json]
  # emits: [{"content": ..., "score": ..., "uri": "memory/<file>.md#<chunk>"}, ...]

  Wired into the harness as the memory_external feeder:
    hybrid_recall.py "<q>" --k <k> --json \
      --external-hits <(python3 memory_search_shim.py "<q>" --memory-dir <mem> --k <k> --json)
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

_MODEL_ID = os.environ.get("DINOMEM_EMBED_MODEL", "intfloat/multilingual-e5-small")
_PREFIX_ENABLED = os.environ.get("DINOMEM_EMBED_PREFIX", "1") != "0"


def _log(msg: str) -> None:
    print(f"[memory_search_shim] {msg}", file=sys.stderr)


def _resolve_local_model() -> tuple[str, bool]:
    """Mirror session_search's local-model resolution so the shim uses the SAME
    on-disk snapshot (offline, deterministic) as the other legs."""
    import pathlib
    cache_dir = pathlib.Path(os.environ.get("HF_HOME") or
                             (pathlib.Path.home() / ".cache/huggingface")) / "hub"
    model_root = cache_dir / ("models--" + _MODEL_ID.replace("/", "--")) / "snapshots"
    try:
        snaps = sorted(p for p in model_root.iterdir() if p.is_dir()) if model_root.exists() else []
        local = str(snaps[-1]) if snaps else _MODEL_ID
    except Exception:
        local = _MODEL_ID
    return local, (local != _MODEL_ID)


# ── chunking: mirror how distilled memory is retrieved (per-item paragraphs) ──
_CHUNK_MIN = 40      # chars; skip trivial fragments (headings-only lines)
_CHUNK_MAX = 1200    # chars; cap a chunk so one huge file can't dominate


def _chunk_markdown(text: str) -> list[str]:
    """Split a memory/*.md file into semantic chunks. Blank-line paragraphs are
    the unit (matches how memory items are authored: one fact/note per block).
    Over-long blocks are hard-split at _CHUNK_MAX so embeddings stay focused."""
    blocks = re.split(r"\n\s*\n", text)
    chunks: list[str] = []
    for b in blocks:
        b = b.strip()
        if len(b) < _CHUNK_MIN:
            continue
        if len(b) <= _CHUNK_MAX:
            chunks.append(b)
        else:
            for i in range(0, len(b), _CHUNK_MAX):
                seg = b[i:i + _CHUNK_MAX].strip()
                if len(seg) >= _CHUNK_MIN:
                    chunks.append(seg)
    return chunks


def _collect_chunks(memory_dir: Path) -> list[dict]:
    """Every memory/*.md (recursive) -> list of {content, uri}. uri mirrors the
    native tool's citation form: memory/<relpath>#<chunk_index>."""
    units: list[dict] = []
    if not memory_dir.exists():
        return units
    for md in sorted(memory_dir.rglob("*.md")):
        try:
            text = md.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        rel = md.relative_to(memory_dir)
        for ci, chunk in enumerate(_chunk_markdown(text)):
            units.append({"content": chunk,
                          "uri": f"memory/{rel}#{ci}"})
    return units


def search(query: str, memory_dir: Path, k: int) -> list[dict]:
    """Semantic top-k over the lab's distilled memory, SAME model+prefixing as the
    other hybrid legs. Returns [{content, score, uri}] (the --external-hits shape)."""
    units = _collect_chunks(memory_dir)
    if not units:
        return []
    try:
        from sentence_transformers import SentenceTransformer
        import numpy as np
    except Exception as e:  # noqa: BLE001
        _log(f"embedding deps unavailable ({e}); memory_external leg stays dark")
        return []
    local_path, offline = _resolve_local_model()
    model = SentenceTransformer(local_path, device="cpu")
    q_text = ("query: " + query) if _PREFIX_ENABLED else query
    p_texts = [("passage: " + u["content"]) if _PREFIX_ENABLED else u["content"]
               for u in units]
    q_emb = model.encode([q_text], normalize_embeddings=True)[0]
    p_emb = model.encode(p_texts, normalize_embeddings=True, batch_size=32)
    sims = (p_emb @ q_emb)  # cosine (normalized) — same axis as e5 legs
    order = np.argsort(-sims)[:k]
    out = []
    for idx in order:
        out.append({"content": units[idx]["content"],
                    "score": float(sims[idx]),
                    "uri": units[idx]["uri"]})
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="memory_search shim (sandbox memory_external feeder)")
    ap.add_argument("query")
    ap.add_argument("--memory-dir", required=True,
                    help="the lab's memory/ dir (distilled memory/*.md to search)")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    hits = search(args.query, Path(args.memory_dir), args.k)
    if args.json:
        print(json.dumps(hits, ensure_ascii=False))
    else:
        for i, h in enumerate(hits, 1):
            print(f"[{i}] ({h['score']:.3f}) {h['uri']}")
            print("    " + h["content"][:160].replace("\n", " "))


if __name__ == "__main__":
    main()
