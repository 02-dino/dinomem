#!/usr/bin/env python3
"""
rag_recall.py — the PLAIN VECTOR / RAG baseline recall engine (arm=rag).

WHY THIS ARM EXISTS
  Every memory-system paper (Mem0, Zep, Mnemo, ...) reports a naive vector-RAG
  floor: chunk the raw conversation, embed, top-k retrieve, answer. It is the
  "no memory engineering at all" competitor. dinomem-base's accuracy DELTA vs
  THIS floor is the real "does the pipeline actually help?" number — without a
  RAG arm, base only has a lexical-over-distilled-memory story and no honest
  lower bound. So this is the naive competitor baseline, deliberately dumb:
    raw haystack turns  ->  chunk  ->  embed  ->  cosine top-k  ->  answer
  NO dinomem pipeline (no extract / cleanup / review / graph / synthesis /
  promotion). NO distilled memory. Just retrieval over the raw sessions.

HOW IT PLUGS IN (zero new answer loop)
  answer.py already ships a generic `--recall command` hook (FORMAT 2): it shells
  DINOMEM_BENCH_RECALL_CMD, replaces {q}/{k}, and parses stdout as JSONL
  {source,text} (or bare lines). This engine emits EXACTLY that generic shape, so
  the RAG arm reuses answer.py's compose loop, its answer model, its official
  hypothesis emission — IDENTICAL protocol to the base/neuron arms. The ONLY
  variable is the retrieval engine. That shared-protocol discipline is what makes
  the 3-arm comparison fair (step 1c).

INDEX MODEL (ephemeral, per-lab)
  The RAG arm has no "converged memory" to read — its corpus is the raw emitted
  haystack archive(s) (what `adapter.py emit` writes into <lab>/sessions/*.jsonl).
  This script builds an in-process embedding index over the chunked turns of that
  archive on first call and caches it to <lab>/.rag_index/ so subsequent
  per-question calls are cheap. The embedding model is local
  (sentence-transformers, same family the neuron KB uses) — no API spend to build
  or query the index, matching the note's "building it is free" invariant.

USAGE
  # one-shot query (the shape answer.py's --recall command hook calls):
  python3 rag_recall.py query --lab <lab> "<question>" --k 8 --json
  # explicit (re)build of the index (optional; query auto-builds if missing):
  python3 rag_recall.py index --lab <lab> [--force]

  Wire the RAG arm's answer step with:
    DINOMEM_BENCH_RECALL_CMD='python3 <...>/rag_recall.py query --lab <lab> "{q}" --k {k} --json'
    python3 answer.py --lab <lab> --dataset <ds> --out hyp.jsonl --recall command ...

OUTPUT (FORMAT 2 the answer loop parses)
  One JSON object per line on stdout, rank-ordered best-first:
    {"source": "<archive>#s<sess>t<turn>", "text": "<chunk text>", "score": <cos>}
  answer.py maps content->text, source->source; score is advisory (ignored by the
  loop, kept for retrieval-metric use in step 1b).

DETERMINISM
  Embedding is deterministic for a fixed model + input; cosine top-k is stable
  with a tie-break on (‑score, source) so repeated calls emit identical order —
  required by the harness determinism guard.

G1 (no-TA), read-only over the lab: this NEVER writes into the live workspace; it
only builds a cache under the lab's own .rag_index/ (guarded by the .dinomem_lab
sentinel, same isolation contract as the rest of the harness).
"""
import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

# Local embedding model — same multilingual-e5-small family the neuron KB uses,
# overridable so a user can pin whatever their KB uses. Kept LOCAL (no API) so the
# RAG arm is free to build/query and needs no network at query time.
DEFAULT_EMBED_MODEL = os.environ.get(
    "DINOMEM_BENCH_RAG_EMBED_MODEL", "intfloat/multilingual-e5-small"
)
# Chunking: LongMemEval turns are already short-ish; we chunk long turns into
# word windows so a single verbose turn doesn't dominate a chunk. Small overlap
# preserves context that straddles a boundary.
CHUNK_WORDS = int(os.environ.get("DINOMEM_BENCH_RAG_CHUNK_WORDS", "120"))
CHUNK_OVERLAP = int(os.environ.get("DINOMEM_BENCH_RAG_CHUNK_OVERLAP", "20"))


def _fail(msg: str) -> None:
    print(f"rag_recall: ERROR: {msg}", file=sys.stderr)
    sys.exit(2)


def _log(msg: str) -> None:
    print(f"rag_recall: {msg}", file=sys.stderr)


def _assert_lab(lab: Path) -> None:
    if not (lab / ".dinomem_lab").exists():
        _fail(f"{lab} is not a harness lab (missing .dinomem_lab sentinel)")


# ── corpus: read the emitted haystack archive(s), chunk their turns ──────────
def _iter_archives(lab: Path):
    sess_dir = lab / "sessions"
    if not sess_dir.is_dir():
        _fail(f"no sessions dir in lab {lab} (did adapter.py emit run?)")
    files = sorted(sess_dir.glob("*.jsonl"))
    if not files:
        _fail(f"no *.jsonl archives in {sess_dir} (did adapter.py emit run?)")
    return files


def _chunk_words(text: str):
    words = text.split()
    if len(words) <= CHUNK_WORDS:
        return [text] if text.strip() else []
    out = []
    step = max(1, CHUNK_WORDS - CHUNK_OVERLAP)
    for start in range(0, len(words), step):
        piece = " ".join(words[start:start + CHUNK_WORDS])
        if piece.strip():
            out.append(piece)
        if start + CHUNK_WORDS >= len(words):
            break
    return out


def build_corpus(lab: Path) -> list[dict]:
    """Flatten every emitted archive's message turns into chunk records.

    Each record: {source, text}. source encodes archive + session + turn so a
    retrieval hit is traceable back to the exact haystack turn (needed for the
    step-1b retrieval-precision metric against answer_session_ids).
    """
    records: list[dict] = []
    for arch in _iter_archives(lab):
        name = arch.name
        sess_idx = 0
        turn_idx = 0
        prev_role = None
        for line in arch.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") == "session":
                # header — reset per-archive counters
                sess_idx = 0
                turn_idx = 0
                prev_role = None
                continue
            if rec.get("type") != "message":
                continue
            msg = rec.get("message") or {}
            role = msg.get("role", "user")
            # gold-attribution: the emitted session id this turn belongs to
            # (adapter stamps session_id/session_idx per message). Falls back
            # to a synthetic s<idx> if an older archive lacks the tag.
            sid = rec.get("session_id")
            if sid is None:
                sid = f"s{rec.get('session_idx', sess_idx)}"
            # a new session boundary in the emit is a user turn following an
            # assistant turn is NOT reliable; the adapter concatenates sessions
            # into one archive without a per-session marker, so we track a
            # monotonic turn index and derive a coarse session index from the
            # emitted timestamp changes below. Keep it simple + traceable.
            parts = msg.get("content") or []
            text = " ".join(
                p.get("text", "") for p in parts if isinstance(p, dict)
            ).strip()
            if not text:
                turn_idx += 1
                prev_role = role
                continue
            ts = rec.get("timestamp", "")
            for ci, chunk in enumerate(_chunk_words(text)):
                suffix = f"c{ci}" if ci else ""
                records.append({
                    "source": f"{name}#t{turn_idx}{suffix}",
                    "text": chunk,
                    "role": role,
                    "ts": ts,
                    "session_id": str(sid),
                })
            turn_idx += 1
            prev_role = role
    if not records:
        _fail("corpus is empty after chunking (archives had no text turns)")
    return records


# ── embedding index (cached under <lab>/.rag_index) ──────────────────────────
def _index_dir(lab: Path) -> Path:
    return lab / ".rag_index"


def _corpus_signature(records: list[dict], model_name: str) -> str:
    h = hashlib.sha256()
    h.update(model_name.encode())
    h.update(str(CHUNK_WORDS).encode())
    h.update(str(CHUNK_OVERLAP).encode())
    for r in records:
        h.update(r["source"].encode())
        h.update(r["text"].encode())
    return h.hexdigest()


def _load_embedder(model_name: str):
    try:
        from sentence_transformers import SentenceTransformer
    except Exception as e:  # pragma: no cover
        _fail(f"sentence-transformers not available: {e}")
    try:
        return SentenceTransformer(model_name)
    except Exception as e:
        _fail(f"could not load embed model {model_name}: {e}")


def build_index(lab: Path, model_name: str, force: bool = False) -> dict:
    """Embed the corpus and cache vectors + records under <lab>/.rag_index/.

    Cache is keyed by a corpus+model signature so a stale index is rebuilt
    automatically when the corpus or model changes.
    """
    try:
        import numpy as np
    except Exception as e:  # pragma: no cover
        _fail(f"numpy not available: {e}")
    records = build_corpus(lab)
    sig = _corpus_signature(records, model_name)
    idir = _index_dir(lab)
    idir.mkdir(parents=True, exist_ok=True)
    meta_path = idir / "meta.json"
    vec_path = idir / "vectors.npy"
    rec_path = idir / "records.jsonl"

    if not force and meta_path.exists() and vec_path.exists() and rec_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
            if meta.get("signature") == sig:
                return {"cached": True, "n": meta.get("n", 0),
                        "model": model_name, "index_dir": str(idir)}
        except Exception:
            pass  # fall through to rebuild

    model = _load_embedder(model_name)
    texts = [r["text"] for r in records]
    # e5 family expects a "passage: " prefix for documents.
    passages = [f"passage: {t}" for t in texts]
    _log(f"embedding {len(passages)} chunk(s) with {model_name} ...")
    embs = model.encode(passages, normalize_embeddings=True,
                        show_progress_bar=False, batch_size=64)
    embs = np.asarray(embs, dtype="float32")
    np.save(vec_path, embs)
    with rec_path.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")
    meta_path.write_text(json.dumps({
        "signature": sig, "n": len(records), "model": model_name,
        "dim": int(embs.shape[1]) if embs.ndim == 2 else 0,
        "chunk_words": CHUNK_WORDS, "chunk_overlap": CHUNK_OVERLAP,
    }, indent=2))
    return {"cached": False, "n": len(records), "model": model_name,
            "index_dir": str(idir)}


def _load_index(lab: Path):
    import numpy as np
    idir = _index_dir(lab)
    vec_path = idir / "vectors.npy"
    rec_path = idir / "records.jsonl"
    meta_path = idir / "meta.json"
    if not (vec_path.exists() and rec_path.exists() and meta_path.exists()):
        return None
    vecs = np.load(vec_path)
    records = [json.loads(l) for l in rec_path.read_text().splitlines() if l.strip()]
    meta = json.loads(meta_path.read_text())
    return vecs, records, meta


# ── query: cosine top-k, emit FORMAT-2 JSONL ─────────────────────────────────
def query(lab: Path, question: str, k: int, model_name: str) -> list[dict]:
    import numpy as np
    idx = _load_index(lab)
    if idx is None:
        build_index(lab, model_name, force=False)
        idx = _load_index(lab)
        if idx is None:
            _fail("index build failed (no vectors after build)")
    vecs, records, meta = idx
    # verify the cached index matches the current corpus+model; rebuild if drifted
    live_records = build_corpus(lab)
    if _corpus_signature(live_records, model_name) != meta.get("signature"):
        _log("index signature drifted vs corpus — rebuilding")
        build_index(lab, model_name, force=True)
        vecs, records, meta = _load_index(lab)

    model = _load_embedder(model_name)
    q_emb = model.encode([f"query: {question}"], normalize_embeddings=True,
                        show_progress_bar=False)
    q = np.asarray(q_emb, dtype="float32")[0]
    # vectors are L2-normalized -> dot product == cosine similarity
    sims = vecs @ q
    order = np.argsort(-sims)  # descending
    out = []
    seen_src = set()
    for i in order:
        rec = records[int(i)]
        src = rec["source"]
        if src in seen_src:
            continue
        seen_src.add(src)
        out.append({"source": src, "text": rec["text"],
                    "session_id": rec.get("session_id", ""),
                    "score": round(float(sims[int(i)]), 6)})
        if len(out) >= k:
            break
    # deterministic tie-break: stable sort on (-score, source)
    out.sort(key=lambda r: (-r["score"], r["source"]))
    return out[:k]


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Plain vector/RAG baseline recall engine (arm=rag)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("index", help="build/refresh the embedding index over the lab corpus")
    pi.add_argument("--lab", required=True)
    pi.add_argument("--model", default=DEFAULT_EMBED_MODEL)
    pi.add_argument("--force", action="store_true")

    pq = sub.add_parser("query", help="top-k retrieve; emit FORMAT-2 JSONL on stdout")
    pq.add_argument("--lab", required=True)
    pq.add_argument("question", help="the question text (answer.py substitutes {q})")
    pq.add_argument("--k", type=int, default=8)
    pq.add_argument("--model", default=DEFAULT_EMBED_MODEL)
    pq.add_argument("--json", action="store_true",
                    help="accepted for interface parity; output is always JSONL")

    args = ap.parse_args()
    lab = Path(args.lab).resolve()
    _assert_lab(lab)

    if args.cmd == "index":
        info = build_index(lab, args.model, force=args.force)
        _log(f"index {'(cached)' if info['cached'] else 'built'}: "
             f"n={info['n']} model={info['model']} dir={info['index_dir']}")
        return

    if args.cmd == "query":
        units = query(lab, args.question, args.k, args.model)
        for u in units:
            # FORMAT 2 the answer loop parses: {source, text}(+score advisory)
            print(json.dumps(u))
        return


if __name__ == "__main__":
    main()
