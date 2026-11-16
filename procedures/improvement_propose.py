#!/usr/bin/env python3
"""
improvement_propose.py — L2 queue reader for dinomem auto self-improvement.

WHY THIS IS NOT THE LLM: dinomem has no in-process model caller, and inventing
one would bypass the gateway's own model routing. So L2 is split in two:
  * THIS script  — zero-LLM. Claims the next candidate and prints a ready-to-read
                   payload. Deterministic, idempotent, safe to run twice.
  * the cron job — `--message` on the PRIMARY model. That prompt is the actual
                   reasoning half, and it names the build stack explicitly
                   (build-quality -> route.py classify -> leaf writer ->
                   verify.sh -> route.py dup -> route.py verify).

WHAT IT NEVER DOES: it does not open session files (L1 already extracted a
<=6 KB excerpt; raw spans reach 1.9 MB, past a context window), and it never
writes to a live surface. Proposals land in pending/ and wait for the owner.

CLAIM MODEL: `next` moves a candidate new -> claimed and stamps claimed_at.
A claim older than CLAIM_TTL_SEC is reclaimable, so a cron that dies mid-run
cannot strand work forever. Fail-closed: an unreadable candidate is skipped,
never silently treated as empty.

Usage:
  improvement_propose.py next [--json]      # claim + print the next candidate
  improvement_propose.py list               # queue overview
  improvement_propose.py show <id>          # one candidate (claims nothing)
  improvement_propose.py done <id>          # mark proposed (leaves pending/ alone)
  improvement_propose.py drop <id> [--why]  # no lesson here; close it out
  improvement_propose.py release <id>       # undo a claim
  improvement_propose.py expire [--json]    # drop stale `new` candidates (zero-LLM)
  improvement_propose.py selftest           # offline checks for expire

Exit codes: 0 = ok, 1 = nothing to do / not found (so cron can early-exit).
"""

import argparse
import calendar
import json
import os
import re
import sys
import time
from pathlib import Path

CLAIM_TTL_SEC = 3 * 3600   # a dead cron's claim becomes reclaimable after this
# The scan adds ~5 candidates per pass while the worker clears a handful a day,
# so an unbounded queue only grows and the oldest lessons go stale first.
# 0 disables expiry.
try:
    EXPIRE_DAYS = max(0, int(os.environ.get("DINOMEM_IMPROVE_EXPIRE_DAYS", "14")))
except ValueError:
    EXPIRE_DAYS = 14
_ISO_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")

_WS_DEFAULT = "DINOMEM_WORKSPACE_PLACEHOLDER"
if _WS_DEFAULT.startswith("DINOMEM_"):
    _WS_DEFAULT = str(Path(__file__).resolve().parent.parent)
WORKSPACE = Path(os.environ.get("DINOMEM_WORKSPACE", _WS_DEFAULT))

QUEUE_DIR = WORKSPACE / "kb" / "improvement_queue"
PENDING_DIR = WORKSPACE / "kb" / "improvement_pending"
SKILL_USAGE_NAME = "skill_usage.json"


def _candidates():
    """Every queue file, oldest first. Skips the usage sidecar and junk."""
    if not QUEUE_DIR.is_dir():
        return []
    out = []
    for path in sorted(QUEUE_DIR.glob("*.json")):
        if path.name == SKILL_USAGE_NAME:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue  # fail-closed: unreadable != empty, just not actionable
        if isinstance(data, dict) and data.get("id"):
            out.append((path, data))
    out.sort(key=lambda t: t[1].get("detected_at", 0))
    return out


def _is_claimable(data, now):
    status = data.get("status", "new")
    if status == "new":
        return True
    if status == "claimed":
        return (now - int(data.get("claimed_at", 0))) > CLAIM_TTL_SEC
    return False  # proposed / dropped are terminal


def _save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8")


def _find(cand_id):
    for path, data in _candidates():
        if data.get("id") == cand_id:
            return path, data
    return None, None


def _set_status(cand_id, status, **extra):
    """Terminal/!terminal status transition. Returns (ok, message)."""
    path, data = _find(cand_id)
    if not path:
        return False, f"no candidate {cand_id!r} in queue"
    data["status"] = status
    data.update(extra)
    _save(path, data)
    return True, f"{cand_id} -> {status}"


# ── Operations ───────────────────────────────────────────────────────────
def op_next(as_json):
    """Claim the oldest claimable candidate and print it.

    Oldest-first on purpose: a lesson decays. A candidate that keeps losing to
    newer ones would never be processed, which is the starvation bug that makes
    a queue quietly useless.
    """
    now = int(time.time())
    for path, data in _candidates():
        if not _is_claimable(data, now):
            continue
        data["status"] = "claimed"
        data["claimed_at"] = now
        _save(path, data)
        return _emit(data, as_json)
    msg = "no claimable candidate"
    print(json.dumps({"ok": True, "candidate": None, "reason": msg})
          if as_json else msg)
    return 1  # non-zero so the cron gate can early-exit without a model call


def _print_payload(data):
    """Human/prompt-readable rendering. This is what the cron prompt reads."""
    ev = data.get("evidence", {})
    meta = ev.get("excerpt_meta", {})
    print(f"CANDIDATE {data['id']}")
    print(f"signals   : {'+'.join(data.get('signals', []))}")
    print(f"source    : {Path(ev.get('session_file','?')).name}")
    print(f"span      : L{ev.get('line_start')}-{ev.get('line_end')}"
          f"  ({meta.get('events_total','?')} events"
          f"{', truncated' if meta.get('truncated') else ''})")
    print("--- EXCERPT (this is the ONLY transcript you need; do NOT open the session file) ---")
    for ev_item in ev.get("excerpt", []):
        role = ev_item.get("role", "?")
        if role == "tool":
            print(f"  [tool] {ev_item.get('tool','')}: {ev_item.get('args','')}")
        else:
            print(f"  [{role}] {ev_item.get('text','')}")
    print("--- END EXCERPT ---")


def _emit(data, as_json):
    """THE single candidate renderer, shared by `next` and `show`.

    WHY ONE FUNCTION: the cron prompt parses this payload. Two copies would be
    free to drift, and the drift would be SILENT — `show` would keep looking
    right to a human while `next` fed the model a different shape.
    """
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        _print_payload(data)
    return 0


def op_list(as_json):
    rows = []
    for _, data in _candidates():
        rows.append({"id": data.get("id"), "status": data.get("status", "new"),
                     "signals": data.get("signals", []),
                     "source": Path(data.get("evidence", {})
                                    .get("session_file", "?")).name})
    if as_json:
        print(json.dumps({"ok": True, "count": len(rows), "items": rows},
                         ensure_ascii=False, indent=2))
    else:
        print(f"queue: {len(rows)} candidate(s)  ({QUEUE_DIR})")
        for r in rows:
            print(f"  [{r['id']}] {r['status']:9s} {'+'.join(r['signals']):28s} {r['source'][:40]}")
    return 0 if rows else 1


def op_due(as_json=False):
    """Is there work? Exit 0 = yes, 1 = no. CLAIMS NOTHING.

    Separate from `next` on purpose: the cron dispatcher asks this question
    every tick, but the claim must happen inside the worker turn. If the gate
    claimed, a dispatch that never reached a model would still burn the
    candidate and leave it stranded until its TTL expired.
    """
    now = int(time.time())
    ready = [d for _, d in _candidates() if _is_claimable(d, now)]
    if as_json:
        print(json.dumps({"ok": True, "due": len(ready),
                          "ids": [d["id"] for d in ready[:5]]}))
    elif ready:
        print(f"{len(ready)} candidate(s) due")
    return 0 if ready else 1


def _candidate_epoch(path, data):
    """When did the evidence happen? First ISO date in the session file name
    (a leading date = session start; reset/orphan archives only carry the
    archive date in a `.archived.<kind>.<ISO>` suffix), else detected_at,
    else the queue file's mtime. Never raises: a missing age is 'young'."""
    name = Path(str((data.get("evidence") or {}).get("session_file") or "")).name
    m = _ISO_DATE.search(name)
    if m:
        try:
            return calendar.timegm((int(m[1]), int(m[2]), int(m[3]), 0, 0, 0))
        except (ValueError, OverflowError):
            pass
    try:
        ts = int(data.get("detected_at") or 0)
    except (TypeError, ValueError):
        ts = 0
    if ts > 0:
        return ts
    try:
        return int(path.stat().st_mtime)
    except OSError:
        return int(time.time())


def op_expire(as_json=False, now=None, days=None):
    """Drop `new` candidates older than EXPIRE_DAYS. Zero-LLM, idempotent.
    Only `new` is touched: `claimed` may be mid-run and `proposed` waits for
    the human. Always exits 0 (a gate step that must never wedge the lane)."""
    now = int(time.time()) if now is None else now
    days = EXPIRE_DAYS if days is None else days
    expired = []
    if days > 0:
        cutoff = now - days * 86400
        for path, data in _candidates():
            if data.get("status", "new") != "new":
                continue
            if _candidate_epoch(path, data) < cutoff:
                data["status"] = "dropped"
                data["dropped_reason"] = f"expired: >{days}d unreviewed"
                data["dropped_at"] = now
                _save(path, data)
                expired.append(data["id"])
    if as_json:
        print(json.dumps({"ok": True, "expired": len(expired), "ids": expired,
                          "days": days}))
    elif expired:
        print(f"expired {len(expired)} candidate(s) older than {days}d")
    return 0


def op_selftest():
    """Offline: expiry ages by session-name date, then detected_at, and never
    touches claimed/proposed. Runs against a temp queue, not the real one."""
    import tempfile
    global QUEUE_DIR
    real = QUEUE_DIR
    now = calendar.timegm((2026, 11, 16, 0, 0, 0))
    day = 86400
    cases = {  # id: (status, session_file, detected_at, expect_status)
        "old_lead": ("new", "/s/2026-10-01T06-23-11Z_ab.jsonl", now, "dropped"),
        "old_sfx": ("new", "/s/ab-cd.archived.reset.2026-10-20T01-00-00.jsonl", now, "dropped"),
        "young": ("new", "/s/x.archived.orphan.2026-11-10T07-23-04.jsonl", 0, "new"),
        "nodate_old": ("new", "/s/abcdef.jsonl", now - 30 * day, "dropped"),
        "nodate_young": ("new", "/s/abcdef.jsonl", now - 2 * day, "new"),
        "proposed_old": ("proposed", "/s/2026-01-01_x.jsonl", 0, "proposed"),
        "claimed_old": ("claimed", "/s/2026-01-01_x.jsonl", 0, "claimed"),
    }
    fails = []
    with tempfile.TemporaryDirectory() as td:
        QUEUE_DIR = Path(td)
        try:
            for cid, (st, sf, det, _) in cases.items():
                (QUEUE_DIR / f"{cid}.json").write_text(json.dumps(
                    {"id": cid, "status": st, "detected_at": det,
                     "claimed_at": now, "evidence": {"session_file": sf}}))
            (QUEUE_DIR / SKILL_USAGE_NAME).write_text("{}")
            op_expire(now=now, days=14)
            op_expire(now=now, days=0)  # disabled -> no-op
            for cid, (*_, want) in cases.items():
                got = json.loads((QUEUE_DIR / f"{cid}.json").read_text())
                if got["status"] != want:
                    fails.append(f"{cid}: {got['status']} != {want}")
                if want == "dropped" and not got.get("dropped_reason", "").startswith("expired: >14d"):
                    fails.append(f"{cid}: reason {got.get('dropped_reason')!r}")
        finally:
            QUEUE_DIR = real
    for f in fails:
        print("FAIL", f, file=sys.stderr)
    print(f"selftest: {len(cases) - len(fails)}/{len(cases)} ok")
    return 1 if fails else 0


def op_show(cand_id, as_json):
    _, data = _find(cand_id)
    if not data:
        print(f"no candidate {cand_id!r}", file=sys.stderr)
        return 1
    return _emit(data, as_json)


def main():
    ap = argparse.ArgumentParser(
        description="L2 queue reader (zero-LLM). The cron prompt is the reasoning half.")
    ap.add_argument("op", choices=["due", "next", "list", "show", "done", "drop", "release",
                                   "expire", "selftest"])
    ap.add_argument("id", nargs="?", help="candidate id (show/done/drop/release)")
    ap.add_argument("--why", default="", help="reason recorded on drop")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.op == "due":
        return op_due(args.json)
    if args.op == "expire":
        return op_expire(args.json)
    if args.op == "selftest":
        return op_selftest()
    if args.op == "next":
        return op_next(args.json)
    if args.op == "list":
        return op_list(args.json)

    if not args.id:
        print(f"{args.op} needs a candidate id", file=sys.stderr)
        return 1
    if args.op == "show":
        return op_show(args.id, args.json)

    # Terminal transitions. `done` deliberately does NOT touch pending/ — the
    # proposal lands there via route.py, and this script must never be able to
    # write a live surface or fabricate an approval.
    status = {"done": "proposed", "drop": "dropped", "release": "new"}[args.op]
    extra = {"dropped_reason": args.why} if args.op == "drop" else {}
    if args.op == "release":
        extra["claimed_at"] = 0
    ok, msg = _set_status(args.id, status, **extra)
    print(json.dumps({"ok": ok, "detail": msg}) if args.json else msg,
          file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
