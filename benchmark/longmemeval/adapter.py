#!/usr/bin/env python3
"""
adapter.py — LongMemEval-S dataset adapter for the dinomem harness.

RESPONSIBILITIES
  1. FETCH longmemeval_s ON DEMAND, pinned to the official HF revision SHA, and
     HASH-VERIFY it against a known-good sha256 before use. (Integrity anchor:
     delete-after is only safe because the run records SHA + verifies hash.)
  2. MAP one LongMemEval-S question's `haystack_sessions` -> a dinomem session
     archive .jsonl in the exact shape extract_memory.py scans.
  3. DELETE the payload after use (caller invokes cleanup); KEEP provenance.

INTEGRITY (see results/PROVENANCE.md):
  Dataset:   xiaowu0162/longmemeval  (deprecated in favor of -cleaned, but the
             ORIGINAL is what the papers/leaderboard cite -> we use it for a
             COMPARABLE number; recorded in the stamp).
  Revision:  2ec2a557f339b6c0369619b1ed5793734cc87533  (pinned; never 'main').
  File:      longmemeval_s  (renamed from longmemeval_s.json; no extension).
  sha256:    08d8dad4be43ee2049a22ff5674eb86725d0ce5ff434cde2627e5e8e7e117894
  size:      278025796 bytes
  These are published in HF's public tree API, so anyone can independently
  verify the exact bytes this harness ran on.

ARCHIVE SHAPE (verified against a live dinomem session archive + extract_memory):
  extract_memory.py scans:  SESSIONS_DIR.glob("*.archived.*.jsonl")
  so the emitted filename MUST match *.archived.<iso>.jsonl or extraction finds
  nothing. Record shape per line:
    line 1: {"type":"session","version":3,"id":<uuid>,"timestamp":<iso>,"cwd":<lab>}
    then:   {"type":"message","id":<hex>,"parentId":<prev|null>,"timestamp":<iso>,
             "message":{"role":"user|assistant","content":[{"type":"text","text":..}]}}

USAGE
  python3 adapter.py fetch  --revision <sha> --out <file> [--json]
  python3 adapter.py emit   --dataset <file> --index <n> --lab <lab_dir> [--json]
  python3 adapter.py schema --dataset <file> [--index 0]   # inspect one sample
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

# ── Pinned provenance (the integrity anchor) ────────────────────────────────
HF_REPO = "xiaowu0162/longmemeval"
HF_REVISION = "2ec2a557f339b6c0369619b1ed5793734cc87533"
HF_FILE = "longmemeval_s"  # renamed from longmemeval_s.json; no extension
EXPECTED_SHA256 = "08d8dad4be43ee2049a22ff5674eb86725d0ce5ff434cde2627e5e8e7e117894"
EXPECTED_SIZE = 278025796
RESOLVE_URL = (
    f"https://huggingface.co/datasets/{HF_REPO}/resolve/{HF_REVISION}/{HF_FILE}"
)


def _fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(2)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(out: Path, revision: str = HF_REVISION) -> dict:
    """Download longmemeval_s to `out`, then HASH-VERIFY. Fail-loud on mismatch.
    Returns provenance dict (repo/revision/sha256/size) for the run stamp."""
    url = f"https://huggingface.co/datasets/{HF_REPO}/resolve/{revision}/{HF_FILE}"
    out.parent.mkdir(parents=True, exist_ok=True)
    # curl -L follows the 302 -> Xet CDN. Fail-loud on non-zero.
    rc = subprocess.call(
        ["curl", "-sSfL", url, "-o", str(out)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    if rc != 0 or not out.exists():
        _fail(f"download failed (curl rc={rc}) from {url}")

    size = out.stat().st_size
    digest = _sha256_file(out)

    # Integrity gate: verify BOTH size and sha256 against the pinned known-good.
    if revision == HF_REVISION:
        if size != EXPECTED_SIZE:
            _fail(f"size mismatch: got {size}, expected {EXPECTED_SIZE} "
                  "-> refusing (possibly wrong/tampered file)")
        if digest != EXPECTED_SHA256:
            _fail(f"sha256 mismatch: got {digest}, expected {EXPECTED_SHA256} "
                  "-> refusing (data not canonical)")
        verified = True
    else:
        # A non-pinned revision can't be checked against the shipped hash; record
        # what we got but mark unverified so the stamp is honest.
        verified = False

    return {
        "repo": HF_REPO,
        "revision": revision,
        "file": HF_FILE,
        "path": str(out),
        "sha256": digest,
        "size": size,
        "hash_verified": verified,
        "expected_sha256": EXPECTED_SHA256 if revision == HF_REVISION else None,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


# ── Dataset loading + schema ────────────────────────────────────────────────
def load_dataset(path: Path) -> list:
    """LongMemEval-S is a JSON array of question objects. Load and return it."""
    if not path.exists():
        _fail(f"dataset file not found: {path} (run `adapter.py fetch` first)")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        _fail(f"expected a JSON array of questions, got {type(data).__name__}")
    return data


def inspect_schema(sample: dict) -> dict:
    """Report the keys of one sample so the mapping can be verified against the
    REAL data, not assumed. LongMemEval question objects are documented to carry:
    question_id, question_type, question, answer, question_date, and
    haystack_sessions (+ haystack_dates / haystack_session_ids in some releases).
    We read defensively (never assume a key exists)."""
    keys = sorted(sample.keys())
    hs = sample.get("haystack_sessions")
    hs_shape = None
    if isinstance(hs, list) and hs:
        first = hs[0]
        if isinstance(first, list) and first:
            hs_shape = {"sessions": len(hs), "turn_keys": sorted(first[0].keys())
                        if isinstance(first[0], dict) else str(type(first[0]))}
        else:
            hs_shape = {"sessions": len(hs), "first_type": str(type(first))}
    return {"keys": keys, "haystack_shape": hs_shape,
            "question_type": sample.get("question_type"),
            "has_dates": "haystack_dates" in sample}


# ── haystack -> dinomem session archive .jsonl ──────────────────────────────
def _hexid() -> str:
    return uuid.uuid4().hex[:8]


def _iso(ts) -> str:
    """Normalize a LongMemEval timestamp to ISO8601 Z. Accepts epoch or string;
    falls back to now if absent (temporal Qs rely on real dates, so we prefer
    the dataset's own haystack_dates when present)."""
    if ts is None:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if isinstance(ts, (int, float)):
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))
    return str(ts)


def emit_archive(sample: dict, lab_dir: Path, index: int,
                 sessions_dir: Path | None = None) -> dict:
    """Write ONE question's haystack_sessions as a dinomem session archive .jsonl
    into <lab_dir>/sessions/, named to match extract_memory's glob
    (*.archived.<iso>.jsonl). Returns {path, sessions, turns, question_id}.

    Mapping: each haystack session (a list of {role, content} turns) becomes a
    run of message records; per-session date -> message timestamps (temporal
    questions depend on these). All sessions of the sample go into ONE archive
    file (extract_memory splits on session boundaries internally)."""
    sessions = sample.get("haystack_sessions") or []
    dates = sample.get("haystack_dates") or []
    # Per-session ids (needed to attribute a retrieved turn back to a gold
    # answer-session for retrieval precision/recall). Fall back to synthetic
    # s<idx> ids if the release omits haystack_session_ids.
    sess_ids = sample.get("haystack_session_ids") or []
    # Gold: which session(s) actually contain the evidence for the answer.
    answer_sess_ids = (sample.get("answer_session_ids")
                       or sample.get("answer_sessions") or [])
    qid = sample.get("question_id", f"idx{index}")

    # extract_memory.py reads a SPECIFIC SESSIONS_DIR (real layout:
    # <lab>/agents/<agent>/sessions). The runner passes it via --sessions-dir so
    # the emitted archive lands exactly where extract globs; without it we fall
    # back to the flat <lab>/sessions convention (older adapter behavior).
    sess_dir = Path(sessions_dir) if sessions_dir else (lab_dir / "sessions")
    sess_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%dT%H-%M-%S", time.gmtime())
    out = sess_dir / f"{qid}.archived.{stamp}.jsonl"

    lines = []
    # session header line
    lines.append(json.dumps({
        "type": "session", "version": 3, "id": str(uuid.uuid4()),
        "timestamp": _iso(dates[0] if dates else None), "cwd": str(lab_dir),
    }))

    prev = None
    turn_count = 0
    emitted_sess_ids = []
    for si, sess in enumerate(sessions):
        sdate = dates[si] if si < len(dates) else None
        # session id: dataset's own if present, else synthetic stable s<idx>
        sid = str(sess_ids[si]) if si < len(sess_ids) else f"s{si}"
        emitted_sess_ids.append(sid)
        if not isinstance(sess, list):
            continue
        # Per-session boundary marker so extract_memory's chunk splitter can break
        # the (single) archive on real session edges instead of collapsing all
        # haystack sessions into one oversized chunk. extract accepts type
        # 'session_start' (and 'session') as a boundary.
        lines.append(json.dumps({
            "type": "session_start", "session_idx": si, "session_id": sid,
            "timestamp": _iso(sdate),
        }))
        for turn in sess:
            if not isinstance(turn, dict):
                continue
            role = turn.get("role", "user")
            text = turn.get("content", turn.get("text", ""))
            mid = _hexid()
            lines.append(json.dumps({
                "type": "message", "id": mid, "parentId": prev,
                "timestamp": _iso(sdate),
                # session_idx + session_id let a retrieved chunk be attributed
                # back to a gold answer-session (retrieval precision/recall).
                "session_idx": si, "session_id": sid,
                "message": {"role": role,
                            "content": [{"type": "text", "text": text}]},
            }))
            prev = mid
            turn_count += 1

    out.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Gold sidecar: which session ids hold the answer evidence, plus the full
    # emitted-session-id list. score.py's retrieval metrics read this to compute
    # precision/recall of a retrieval set against the gold answer-sessions.
    gold = {
        "question_id": qid,
        "answer_session_ids": [str(x) for x in answer_sess_ids],
        "emitted_session_ids": emitted_sess_ids,
        "n_sessions": len(sessions),
    }
    (sess_dir / f"{qid}.gold.json").write_text(
        json.dumps(gold, indent=2), encoding="utf-8")

    return {"path": str(out), "sessions": len(sessions),
            "turns": turn_count, "question_id": qid,
            "answer_session_ids": gold["answer_session_ids"],
            "question": sample.get("question"),
            "answer": sample.get("answer"),
            "question_type": sample.get("question_type"),
            "question_date": sample.get("question_date")}


def _smoke_validate(path: Path) -> dict:
    """Read the emitted archive back: every line valid JSON, header present,
    at least one message. Mirrors what extract_memory expects."""
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    if not lines:
        _fail(f"emitted archive {path} is empty")
    header = json.loads(lines[0])
    if header.get("type") != "session":
        _fail(f"first line is not a session header: {header.get('type')}")
    msgs = 0
    for ln in lines[1:]:
        if not ln.strip():
            continue
        rec = json.loads(ln)  # raises if invalid -> fail-loud
        if rec.get("type") == "message":
            msgs += 1
    if msgs == 0:
        _fail(f"archive {path} has no message records")
    return {"lines": len(lines), "messages": msgs, "valid": True}


def main() -> None:
    ap = argparse.ArgumentParser(description="LongMemEval-S adapter (fetch/emit/schema)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pf = sub.add_parser("fetch", help="download + hash-verify the dataset")
    pf.add_argument("--out", required=True)
    pf.add_argument("--revision", default=HF_REVISION)
    pf.add_argument("--json", action="store_true")

    pe = sub.add_parser("emit", help="emit one sample as a session archive .jsonl")
    pe.add_argument("--dataset", required=True)
    pe.add_argument("--index", type=int, default=0)
    pe.add_argument("--lab", required=True)
    pe.add_argument("--sessions-dir", default=None,
                    help="exact dir to write the archive into (matches "
                         "extract_memory's SESSIONS_DIR); default <lab>/sessions")
    pe.add_argument("--json", action="store_true")

    ps = sub.add_parser("schema", help="inspect one sample's schema")
    ps.add_argument("--dataset", required=True)
    ps.add_argument("--index", type=int, default=0)

    args = ap.parse_args()

    if args.cmd == "fetch":
        info = fetch(Path(args.out), args.revision)
        print(json.dumps(info, indent=2) if args.json else
              f"fetched {info['file']} sha256={info['sha256'][:16]}... "
              f"verified={info['hash_verified']}")
    elif args.cmd == "schema":
        data = load_dataset(Path(args.dataset))
        print(json.dumps(inspect_schema(data[args.index]), indent=2))
    elif args.cmd == "emit":
        data = load_dataset(Path(args.dataset))
        if not 0 <= args.index < len(data):
            _fail(f"index {args.index} out of range (0..{len(data)-1})")
        info = emit_archive(data[args.index], Path(args.lab), args.index,
                            sessions_dir=Path(args.sessions_dir) if args.sessions_dir else None)
        info["smoke"] = _smoke_validate(Path(info["path"]))
        print(json.dumps(info, indent=2) if args.json else
              f"emitted {info['path']} ({info['sessions']} sessions, "
              f"{info['turns']} turns) smoke={info['smoke']['valid']}")


if __name__ == "__main__":
    main()
