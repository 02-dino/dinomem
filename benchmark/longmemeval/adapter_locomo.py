#!/usr/bin/env python3
"""
adapter_locomo.py — LoCoMo dataset adapter (step 1d).

WHY A SECOND DATASET
  LongMemEval-S and LoCoMo are the two standard long-term-memory QA benchmarks
  (Mem0/Zep/etc. report BOTH). Running both arms on both datasets guards against
  overfitting a memory engine to one benchmark's quirks. LoCoMo is structurally
  DIFFERENT from LongMemEval, so it needs its own mapper — but it emits the SAME
  session-archive .jsonl shape, so rag_recall.py / answer.py / score.py are reused
  UNCHANGED. Only the fetch + haystack->archive + gold mapping differ.

LoCoMo SHAPE (verified against snap-research/locomo data/locomo10.json):
  top level = LIST of conversations. Each conversation:
    - "conversation": { "speaker_a", "speaker_b",
                        "session_1": [turn, ...], "session_1_date_time": "<human>",
                        "session_2": [...], "session_2_date_time": ..., ... }
        a turn = { "speaker": <name>, "dia_id": "D<sess>:<turn>", "text": <str>,
                   (optional "img_url"/"blip_caption" for multimodal turns) }
    - "qa": [ { "question", "answer" (str OR int), "evidence": ["D1:3", ...],
               "category": <int 1..5> } ]
  KEY DIFFERENCES vs LongMemEval:
    * ONE conversation carries MANY questions (LongMemEval = 1 Q per haystack).
    * GOLD is turn-level EVIDENCE dia_ids (not session-level answer_session_ids).
    * category is an INT; adversarial answers are often "Not mentioned"/"No info".
    * session date_time is a HUMAN string ("1:56 pm on 8 May, 2023").

MAPPING (one conversation -> one lab)
  Each conversation's sessions -> ONE session-archive .jsonl (same 3-field message
  shape the pipeline scans), every turn tagged with its dia_id + session_id so a
  retrieved chunk is attributable to gold EVIDENCE. Per QA we write a gold sidecar
  <qid>.gold.json carrying evidence dia_ids (turn-level gold) so score.py's
  retrieval metric works at evidence granularity for LoCoMo, session granularity
  for LongMemEval — same recall/precision math, different gold key.

  question_id is synthesized: "<sample_id>_q<idx>". category int -> a readable
  LoCoMo category name for per-category accuracy.

INTEGRITY / EPHEMERAL (same contract as adapter.py)
  fetch(): pin the source revision (GitHub raw @ commit SHA), download, sha256,
  RECORD it. Payload is deleted after use by run.py; the recorded SHA is the
  re-fetch anchor. locomo10.json is small (~a few MB), single file, no rename.

USAGE
  python3 adapter_locomo.py fetch  --out <tmp.json> [--revision <sha>]
  python3 adapter_locomo.py schema --dataset <file> [--index 0]
  python3 adapter_locomo.py emit   --dataset <file> --index <n> --lab <lab> [--json]
    -> emits ONE conversation's archive + a gold sidecar PER question in it.
"""
import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

# ── Pinned provenance (integrity anchor) ─────────────────────────────────────
# LoCoMo ships as a single JSON in the snap-research/locomo repo. Pin to a commit
# SHA (never a moving branch) so the fetched bytes are reproducible. If the SHA is
# later confirmed, set EXPECTED_SHA256 to hard-verify like adapter.py does.
LOCOMO_REPO = "snap-research/locomo"
LOCOMO_REVISION = "main"  # TODO(pin): replace with a commit SHA once confirmed
LOCOMO_FILE = "data/locomo10.json"
EXPECTED_SHA256 = None  # set to hard-verify once a commit SHA is pinned
RESOLVE_URL = (
    f"https://raw.githubusercontent.com/{LOCOMO_REPO}/{LOCOMO_REVISION}/{LOCOMO_FILE}"
)

# LoCoMo category int -> readable name (matches the paper's 5 reasoning types).
# The exact int->name map is asserted from the dataset where possible; this is the
# documented default (snap-research/locomo evaluation).
CATEGORY_NAMES = {
    1: "multi-hop",
    2: "temporal",
    3: "open-domain",       # commonsense / world-knowledge
    4: "single-hop",
    5: "adversarial",
}


def _fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(2)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _hexid() -> str:
    return uuid.uuid4().hex


def fetch(out: Path, revision: str = LOCOMO_REVISION) -> dict:
    """Download locomo10.json to `out`, sha256, record. Fail-loud on download
    error. Hard-verify only if EXPECTED_SHA256 is pinned for this revision."""
    url = f"https://raw.githubusercontent.com/{LOCOMO_REPO}/{revision}/{LOCOMO_FILE}"
    out.parent.mkdir(parents=True, exist_ok=True)
    rc = subprocess.call(
        ["curl", "-sSfL", url, "-o", str(out)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    if rc != 0 or not out.exists():
        _fail(f"download failed (curl rc={rc}) from {url}")
    size = out.stat().st_size
    digest = _sha256_file(out)
    verified = False
    if EXPECTED_SHA256 and revision == LOCOMO_REVISION:
        if digest != EXPECTED_SHA256:
            _fail(f"sha256 mismatch: got {digest}, expected {EXPECTED_SHA256} "
                  "-> refusing (data not canonical)")
        verified = True
    return {
        "repo": LOCOMO_REPO,
        "revision": revision,
        "file": LOCOMO_FILE,
        "sha256": digest,
        "size": size,
        "hash_verified": verified,
        "expected_sha256": EXPECTED_SHA256,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "note": ("revision is a moving branch unless a commit SHA is pinned; "
                 "sha256 is still recorded for this fetch."),
    }


def load_dataset(path: Path) -> list:
    if not path.exists():
        _fail(f"dataset file not found: {path} (run `adapter_locomo.py fetch` first)")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        _fail(f"expected a JSON array of conversations, got {type(data)}")
    return data


# ── session date_time parsing ────────────────────────────────────────────────
_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], 1)}


def _parse_dt(s: str) -> str:
    """LoCoMo session_N_date_time is human, e.g. '1:56 pm on 8 May, 2023'.
    Parse to ISO8601 for temporal-question fidelity. Fail-soft to a stable
    fallback if the format drifts (never crash the emit)."""
    if not s or not isinstance(s, str):
        return "1970-01-01T00:00:00"
    m = re.search(
        r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*on\s+(\d{1,2})\s+([A-Za-z]+),?\s*(\d{4})",
        s, re.IGNORECASE)
    if not m:
        return "1970-01-01T00:00:00"
    hh = int(m.group(1))
    mm = int(m.group(2) or 0)
    ampm = (m.group(3) or "").lower()
    day = int(m.group(4))
    mon = _MONTHS.get(m.group(5).lower(), 1)
    year = int(m.group(6))
    if ampm == "pm" and hh != 12:
        hh += 12
    if ampm == "am" and hh == 12:
        hh = 0
    return f"{year:04d}-{mon:02d}-{day:02d}T{hh:02d}:{mm:02d}:00"


# ── schema inspect ───────────────────────────────────────────────────────────
def inspect_schema(sample: dict) -> dict:
    conv = sample.get("conversation") or {}
    sess_keys = sorted(k for k in conv if re.fullmatch(r"session_\d+", k))
    first_turn = None
    if sess_keys:
        turns = conv.get(sess_keys[0]) or []
        if turns and isinstance(turns[0], dict):
            first_turn = sorted(turns[0].keys())
    qa = sample.get("qa") or []
    return {
        "sample_id": sample.get("sample_id"),
        "top_keys": sorted(sample.keys()),
        "sessions": len(sess_keys),
        "speakers": [conv.get("speaker_a"), conv.get("speaker_b")],
        "turn_keys": first_turn,
        "n_questions": len(qa),
        "qa_keys": sorted(qa[0].keys()) if qa else None,
        "categories_seen": sorted({q.get("category") for q in qa if "category" in q}),
    }


# ── conversation -> session archive + per-QA gold sidecars ───────────────────
def emit_archive(sample: dict, lab_dir: Path, index: int) -> dict:
    conv = sample.get("conversation") or {}
    sample_id = str(sample.get("sample_id", f"conv{index}"))
    sess_dir = lab_dir / "sessions"
    sess_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%dT%H-%M-%S", time.gmtime())
    out = sess_dir / f"{sample_id}.archived.{stamp}.jsonl"

    # ordered session keys: session_1, session_2, ... (numeric order)
    sess_keys = sorted(
        (k for k in conv if re.fullmatch(r"session_\d+", k)),
        key=lambda k: int(k.split("_")[1]))

    header = {"type": "session", "version": 3, "id": _hexid(),
              "timestamp": _parse_dt(conv.get(f"{sess_keys[0]}_date_time")) if sess_keys
              else "1970-01-01T00:00:00", "cwd": str(lab_dir)}
    lines = [json.dumps(header)]

    prev = None
    turn_count = 0
    dia_to_meta = {}  # dia_id -> {session_id} for gold attribution
    for sk in sess_keys:
        si = int(sk.split("_")[1])
        sid = f"D{si}"                        # LoCoMo dia_ids use D<session>
        sdate = _parse_dt(conv.get(f"{sk}_date_time"))
        turns = conv.get(sk) or []
        if not isinstance(turns, list):
            continue
        for turn in turns:
            if not isinstance(turn, dict):
                continue
            speaker = turn.get("speaker", "user")
            text = turn.get("text", "")
            # multimodal turns may carry a caption instead of/along with text
            if not text and turn.get("blip_caption"):
                text = f"[image] {turn['blip_caption']}"
            dia_id = turn.get("dia_id", "")
            # role: LoCoMo is speaker_a/speaker_b dialog; map both to 'user' so the
            # whole conversation is treated as recallable memory (there is no
            # assistant-authored memory to privilege). Keep speaker in text prefix
            # so speaker attribution survives for multi-hop/temporal questions.
            role = "user"
            body = f"{speaker}: {text}" if speaker else text
            mid = _hexid()
            lines.append(json.dumps({
                "type": "message", "id": mid, "parentId": prev,
                "timestamp": sdate,
                "session_idx": si, "session_id": sid, "dia_id": dia_id,
                "message": {"role": role,
                            "content": [{"type": "text", "text": body}]},
            }))
            prev = mid
            turn_count += 1
            if dia_id:
                dia_to_meta[dia_id] = {"session_id": sid, "session_idx": si}

    out.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # per-QA gold sidecars — turn-level evidence dia_ids are the LoCoMo gold.
    qa = sample.get("qa") or []
    emitted_qids = []
    for qi, q in enumerate(qa):
        qid = f"{sample_id}_q{qi}"
        evidence = [str(e) for e in (q.get("evidence") or [])]
        # session ids the evidence turns live in (for session-granularity metric)
        ev_sessions = sorted({dia_to_meta[e]["session_id"]
                              for e in evidence if e in dia_to_meta})
        cat_int = q.get("category")
        gold = {
            "question_id": qid,
            "question": q.get("question"),
            "answer": str(q.get("answer")),   # answer may be int -> stringify
            "question_type": CATEGORY_NAMES.get(cat_int, f"category_{cat_int}"),
            "category_int": cat_int,
            # DUAL gold keys so score.py works for both datasets:
            "evidence_dia_ids": evidence,             # turn-level (LoCoMo native)
            "answer_session_ids": ev_sessions,        # session-level (shared metric)
            "is_adversarial": cat_int == 5,
        }
        (sess_dir / f"{qid}.gold.json").write_text(
            json.dumps(gold, indent=2), encoding="utf-8")
        emitted_qids.append(qid)

    return {"path": str(out), "sessions": len(sess_keys), "turns": turn_count,
            "sample_id": sample_id, "n_questions": len(qa),
            "question_ids": emitted_qids,
            "categories": sorted({q.get("category") for q in qa if "category" in q})}


def build_question_file(sample: dict, index: int) -> list:
    """Return the answer.py-shaped question list for ONE conversation: one entry
    per QA with {question_id, question, question_type, answer, ...}. answer.py's
    dataset loader can consume a JSON array of these."""
    sample_id = str(sample.get("sample_id", f"conv{index}"))
    qa = sample.get("qa") or []
    out = []
    for qi, q in enumerate(qa):
        cat_int = q.get("category")
        out.append({
            "question_id": f"{sample_id}_q{qi}",
            "question": q.get("question"),
            "answer": str(q.get("answer")),
            "question_type": CATEGORY_NAMES.get(cat_int, f"category_{cat_int}"),
        })
    return out


def _smoke_validate(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        _fail(f"emitted archive {path} is empty")
    header = json.loads(lines[0])
    if header.get("type") != "session":
        _fail("first line is not a session header")
    n_msg = 0
    for l in lines[1:]:
        rec = json.loads(l)
        if rec.get("type") == "message":
            n_msg += 1
    return {"lines": len(lines), "messages": n_msg}


def main() -> None:
    ap = argparse.ArgumentParser(description="LoCoMo adapter (fetch/emit/schema)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pf = sub.add_parser("fetch", help="download locomo10.json (ephemeral) + sha256")
    pf.add_argument("--out", required=True)
    pf.add_argument("--revision", default=LOCOMO_REVISION)

    ps = sub.add_parser("schema", help="inspect one conversation's schema")
    ps.add_argument("--dataset", required=True)
    ps.add_argument("--index", type=int, default=0)

    pe = sub.add_parser("emit", help="emit one conversation as a session archive + gold")
    pe.add_argument("--dataset", required=True)
    pe.add_argument("--index", type=int, required=True)
    pe.add_argument("--lab", required=True)
    pe.add_argument("--json", action="store_true")

    pq = sub.add_parser("questions", help="emit answer.py-shaped question array for one conversation")
    pq.add_argument("--dataset", required=True)
    pq.add_argument("--index", type=int, required=True)
    pq.add_argument("--out", required=True)

    args = ap.parse_args()

    if args.cmd == "fetch":
        info = fetch(Path(args.out), args.revision)
        print(json.dumps(info, indent=2))
        return

    if args.cmd == "schema":
        data = load_dataset(Path(args.dataset))
        print(json.dumps(inspect_schema(data[args.index]), indent=2))
        return

    if args.cmd == "emit":
        data = load_dataset(Path(args.dataset))
        info = emit_archive(data[args.index], Path(args.lab), args.index)
        sv = _smoke_validate(Path(info["path"]))
        info["smoke"] = sv
        if args.json:
            print(json.dumps(info, indent=2))
        else:
            print(f"emitted {info['path']} ({info['sessions']} sessions, "
                  f"{info['turns']} turns, {info['n_questions']} questions); "
                  f"smoke {sv}")
        return

    if args.cmd == "questions":
        data = load_dataset(Path(args.dataset))
        qs = build_question_file(data[args.index], args.index)
        Path(args.out).write_text(json.dumps(qs, indent=2), encoding="utf-8")
        print(f"wrote {len(qs)} questions -> {args.out}")
        return


if __name__ == "__main__":
    main()
