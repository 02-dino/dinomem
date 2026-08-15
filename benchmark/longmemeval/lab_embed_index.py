#!/usr/bin/env python3
"""lab_embed_index.py — build a LAB-LOCAL memory embedding index for the neuron arm.

WHY THIS EXISTS
---------------
The neuron L2/L3 stages (memory_graph.py, memory_synthesis.py) read per-chunk
EMBEDDINGS from a sqlite index resolved via DINOMEM_MEMORY_DB (default: the REAL
production openclaw-agent.sqlite). In production that index is populated by
OpenClaw's *native gateway* memorySearch indexer, which:
  (a) a HEADLESS benchmark lab does not run, and
  (b) stores vectors in a sqlite-vec `vec0` VIRTUAL table (memory_index_chunks_vec)
      that a plain python `sqlite3` cannot read without the vec0 loadable extension.

So in a lab, memory_graph.load_chunks() either hits a missing DB (0 nodes) or, if
DINOMEM_MEMORY_DB is left unset, reads the REAL /root DB — an ISOLATION BREACH
(the benchmark would score over leaked live memory, not the lab haystack).

This stage closes both gaps. It reads the lab's freshly-extracted memory/*.md,
chunks + embeds them with the SAME model the rest of the stack uses
(intfloat/multilingual-e5-small via the TEI server on :8080, e5 `passage:` prefix),
and writes a PLAIN sqlite in exactly the schema _schema_adapter/memory_graph read:

    chunks(id INTEGER, path TEXT, start_line INT, end_line INT,
           text TEXT, embedding TEXT)   -- embedding = json.dumps(list[float])

memory_graph.load_chunks() does `json.loads(emb)` on that column, so a JSON-array
string in a normal column is exactly what it expects (no vec0, no gateway).

CONTRACT
--------
  env DINOMEM_WORKSPACE  -> lab WS (memory/*.md live under WS/memory/)
  env DINOMEM_MEMORY_DB  -> output sqlite path (drive_neuron pins this lab-local)
  env DINOMEM_EMBED_MODEL (default intfloat/multilingual-e5-small)
  env DINOMEM_EMBED_PREFIX (default "1"; e5 asymmetric `passage:` prefix)
  env TEI_EMBED_URL (default http://localhost:8080/embed)

Idempotent: rewrites the chunks table each run. Fail-LOUD: a non-zero exit here
is honest — the neuron arm CANNOT score without embeddings, so a silent empty
index would produce a false low score. G1: no TA, pure data.
"""
import json
import os
import sqlite3
import sys
import urllib.request
from pathlib import Path

WS = Path(os.environ.get("DINOMEM_WORKSPACE", ".")).resolve()
DB_PATH = Path(os.environ["DINOMEM_MEMORY_DB"]) if os.environ.get("DINOMEM_MEMORY_DB") \
    else WS / "kb" / "memory_neuron" / "lab_memory_index.sqlite"
MEM_DIR = WS / "memory"
EMBED_URL = os.environ.get("TEI_EMBED_URL", "http://localhost:8080/embed")
PREFIX_ENABLED = os.environ.get("DINOMEM_EMBED_PREFIX", "1") != "0"

# memory_graph's own firewall set — skip derived/promoted files so the lab index
# matches what the graph would see in prod (no echo-chamber double-count).
FIREWALL_FILES = {"MEMORY.md", "auto_promoted.md"}

# Chunking: memory/*.md are already small item-files (one insight/decision each,
# ~few hundred chars). Chunk by paragraph blocks with a char cap so a long file
# still splits, mirroring how the memory indexer chunks section-wise.
MAX_CHUNK_CHARS = 1200


def log(m: str) -> None:
    print(f"[lab_embed_index] {m}", flush=True)


def _chunk_file(path: Path) -> list[tuple[int, int, str]]:
    """Return (start_line, end_line, text) blocks. Split on blank lines, then
    greedily pack up to MAX_CHUNK_CHARS so vectors stay topically tight."""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    blocks: list[tuple[int, int, str]] = []
    cur: list[str] = []
    cur_start = 1
    for i, ln in enumerate(lines, start=1):
        if ln.strip() == "" and cur:
            text = "\n".join(cur).strip()
            if text:
                blocks.append((cur_start, i - 1, text))
            cur = []
            cur_start = i + 1
        else:
            if not cur:
                cur_start = i
            cur.append(ln)
    if cur:
        text = "\n".join(cur).strip()
        if text:
            blocks.append((cur_start, len(lines), text))
    # greedily merge tiny adjacent blocks up to the char cap
    merged: list[tuple[int, int, str]] = []
    for s, e, t in blocks:
        if merged and len(merged[-1][2]) + len(t) + 1 <= MAX_CHUNK_CHARS:
            ps, _, pt = merged[-1]
            merged[-1] = (ps, e, pt + "\n" + t)
        else:
            merged.append((s, e, t))
    return merged


def _embed_batch(texts: list[str]) -> list[list[float]]:
    """TEI /embed: POST {"inputs": [..]} -> [[float,...], ...]."""
    payload = [("passage: " + t) if PREFIX_ENABLED else t for t in texts]
    req = urllib.request.Request(
        EMBED_URL,
        data=json.dumps({"inputs": payload}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        out = json.loads(r.read().decode("utf-8"))
    if not isinstance(out, list) or not out or not isinstance(out[0], list):
        raise RuntimeError(f"unexpected TEI response shape: {str(out)[:200]}")
    return out


def main() -> int:
    if not MEM_DIR.is_dir():
        log(f"FAIL: no memory dir at {MEM_DIR}")
        return 2
    md_files = sorted(
        p for p in MEM_DIR.glob("*.md") if p.name not in FIREWALL_FILES
    )
    if not md_files:
        log(f"FAIL: no memory/*.md to index under {MEM_DIR} (base extract produced nothing?)")
        return 3

    rows: list[tuple[str, int, int, str]] = []  # (path, start, end, text)
    for f in md_files:
        for s, e, t in _chunk_file(f):
            rows.append((str(f), s, e, t))
    if not rows:
        log("FAIL: memory/*.md present but produced 0 chunks (all empty?)")
        return 4
    log(f"chunking: {len(md_files)} file(s) -> {len(rows)} chunk(s); embedding via {EMBED_URL}")

    # embed in batches (TEI default max batch is generous; keep it modest)
    BATCH = 32
    vecs: list[list[float]] = []
    for i in range(0, len(rows), BATCH):
        batch_texts = [r[3] for r in rows[i:i + BATCH]]
        try:
            vecs.extend(_embed_batch(batch_texts))
        except Exception as ex:  # fail-LOUD: no silent empty index
            log(f"FAIL: TEI embed error on batch {i // BATCH}: {ex}")
            return 5
    if len(vecs) != len(rows):
        log(f"FAIL: embedding count {len(vecs)} != chunk count {len(rows)}")
        return 6
    dim = len(vecs[0]) if vecs else 0
    log(f"embedded {len(vecs)} chunk(s), dim={dim}")

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH))
    try:
        con.execute("DROP TABLE IF EXISTS chunks")
        # Schema matches _schema_adapter aliases: table 'chunks', columns
        # id/path/start_line/end_line/text/embedding. embedding = JSON array str.
        con.execute(
            "CREATE TABLE chunks ("
            "id INTEGER PRIMARY KEY, path TEXT, start_line INTEGER, "
            "end_line INTEGER, text TEXT, embedding TEXT)"
        )
        # a minimal 'files' table too, so the graph's content-signature path works
        con.execute("DROP TABLE IF EXISTS files")
        con.execute("CREATE TABLE files (path TEXT, hash TEXT)")
        seen: dict[str, str] = {}
        for (path, s, e, text), vec in zip(rows, vecs):
            con.execute(
                "INSERT INTO chunks(path, start_line, end_line, text, embedding) "
                "VALUES (?,?,?,?,?)",
                (path, s, e, text, json.dumps(vec)),
            )
            if path not in seen:
                import hashlib
                seen[path] = hashlib.md5(Path(path).read_bytes()).hexdigest()
        for path, h in seen.items():
            con.execute("INSERT INTO files(path, hash) VALUES (?,?)", (path, h))
        con.commit()
    finally:
        con.close()

    log(f"OK: wrote {len(rows)} embedded chunk(s) to {DB_PATH} (schema: chunks/files, embedding=json)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
