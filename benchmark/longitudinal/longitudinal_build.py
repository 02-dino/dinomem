#!/usr/bin/env python3
"""
longitudinal_build.py — Phase 2 (2a): the LONGITUDINAL test-set builder.

WHAT PHASE 2 PROVES (and Phase 1 cannot):
  Phase 1 (LongMemEval/LoCoMo) is a STATIC haystack: all sessions exist up front,
  you retrieve over a fixed corpus. That measures retrieval, not LEARNING.
  Phase 2 asks the different question the strategy PDF demands: does the memory
  corpus get MORE accurate / MORE temporally-coherent as sessions ACCUMULATE, or
  does it degrade (stale facts win, contradictions pile up, recall drifts)?

HOW:
  A scripted timeline of EVOLVING facts across Day1..DayN. Some facts are stable
  (born in Berlin), some SUPERSEDE over time (job: barista -> teacher -> principal;
  city: Berlin -> Munich -> Zurich; pet: none -> cat 'Momo' -> cat died). At each
  CHECKPOINT (after day k) we ask the SAME probe questions whose CORRECT answer is
  whatever was true AS OF that checkpoint. We plot accuracy-vs-sessions PER ARM.
  A good memory system's curve should be FLAT-HIGH or RISING (it tracks the latest
  truth); a naive RAG floor's curve should SAG as superseded facts accumulate and
  pollute retrieval (old 'barista' still retrievable, competes with 'principal').

OUTPUT SHAPE (reuses Phase-1 lab format UNCHANGED so rag_recall/answer/score run):
  <lab>/sessions/<timeline>.archived.<stamp>.jsonl   — one archive, dated turns
  <lab>/sessions/<qid>.gold.json                     — per-probe gold, PER checkpoint
  A checkpoint manifest describing which sessions exist at each probe point, so the
  runner can drive the pipeline INCREMENTALLY (day1, then +day2, ...) and re-probe.

This module only BUILDS the timeline + gold. The incremental drive + probe loop is
longitudinal_run.py. Deterministic (seeded) so runs are reproducible.

Subcommands:
  build   --out <dir> [--seed N] [--days N]   -> timeline.json + checkpoints.json
  emit    --timeline <t.json> --lab <dir> --through-day K  -> archive w/ days<=K only
  gold    --timeline <t.json> --checkpoint K --out <f.json> -> probe set as-of day K
  schema  --timeline <t.json>                 -> human summary of the built timeline
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import time
import uuid
from pathlib import Path

SCHEMA_VERSION = "phase2-2a-2026-08-14"


def _fail(msg: str) -> None:
    raise SystemExit(f"[longitudinal_build] FAIL: {msg}")


def _hexid() -> str:
    return uuid.uuid4().hex[:24]


def _iso(day: int, base_epoch: int) -> str:
    """Day index -> ISO timestamp (one session per day, 12:00 UTC)."""
    t = base_epoch + day * 86400 + 12 * 3600
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t))


# ── the evolving-facts script ────────────────────────────────────────────────
# Each FACT is a track: an attribute whose value may change over time. A fact has
# an id, a probe question, and a list of (day, value) updates. The CORRECT answer
# at checkpoint K = the value of the LATEST update with day <= K. If no update has
# happened yet by day K, the fact is not yet probeable (excluded at that checkpoint).
#
# STABLE facts have exactly one update (never change) -> a memory system should
# always get them; they anchor the floor.
# SUPERSEDING facts change 2-4 times -> they are the discriminator: a system that
# tracks the LATEST truth scores high; a system that lets stale values pollute
# retrieval decays as more supersessions accumulate.
#
# Values are phrased so the fact is stated NATURALLY in a session turn (first
# person, conversational) AND the gold answer is a short canonical string the
# judge can match. state_template renders the day's turn; the gold answer is the
# raw value.
FACTS = [
    # --- STABLE anchors (never change) ---
    {"id": "birthplace", "kind": "stable",
     "q": "Where was I born?",
     "updates": [(1, "Berlin")],
     "tpl": "By the way, I was born in {v}."},
    {"id": "sister_name", "kind": "stable",
     "q": "What is my sister's name?",
     "updates": [(2, "Lena")],
     "tpl": "I called my sister {v} today, we talked for an hour."},
    {"id": "first_car", "kind": "stable",
     "q": "What was my first car?",
     "updates": [(4, "a red Volkswagen Golf")],
     "tpl": "I still remember my first car, {v}."},

    # --- SUPERSEDING discriminators (the real test) ---
    {"id": "job", "kind": "superseding",
     "q": "What is my current job?",
     "updates": [(1, "barista"), (8, "schoolteacher"), (20, "school principal")],
     "tpl": "Update on work: I'm now working as a {v}."},
    {"id": "city", "kind": "superseding",
     "q": "Which city do I currently live in?",
     "updates": [(1, "Berlin"), (6, "Munich"), (15, "Zurich")],
     "tpl": "I just moved — I'm living in {v} now."},
    {"id": "pet", "kind": "superseding",
     "q": "Do I have a pet, and what is it?",
     "updates": [(3, "a cat named Momo"), (12, "two cats, Momo and Biscuit"),
                 (25, "one cat, Biscuit (Momo passed away)")],
     "tpl": "Pet update: I have {v}."},
    {"id": "relationship", "kind": "superseding",
     "q": "What is my current relationship status?",
     "updates": [(2, "single"), (10, "dating someone named Sam"),
                 (22, "engaged to Sam")],
     "tpl": "On the personal front: I'm {v}."},
    {"id": "diet", "kind": "superseding",
     "q": "What diet do I currently follow?",
     "updates": [(5, "vegetarian"), (18, "vegan")],
     "tpl": "Food-wise, I've switched — I'm {v} now."},
]

# Filler turns (memory-irrelevant chatter) so each day's session isn't a single
# suspiciously-clean fact line. Keeps the haystack realistic without adding gold.
FILLER = [
    "The weather was nice today.",
    "I watched a documentary in the evening.",
    "Had a long meeting that ran over.",
    "Tried a new coffee place downtown.",
    "Went for a run this morning.",
    "Read a few chapters of my book.",
    "Fixed a bug that had been bugging me for days.",
    "Cooked dinner instead of ordering in.",
]


def value_asof(fact: dict, day: int):
    """Latest value of `fact` with update-day <= `day`, or None if not yet set."""
    cur = None
    for d, v in fact["updates"]:
        if d <= day:
            cur = v
        else:
            break
    return cur


def build_timeline(seed: int, days: int) -> dict:
    """Return the full timeline spec: per-day turns + the checkpoint schedule.
    Deterministic given (seed, days). base_epoch fixed so timestamps are stable."""
    rng = random.Random(seed)
    base_epoch = 1700000000  # fixed anchor (2023-11) so runs reproduce exactly
    all_days = max(d for f in FACTS for d, _ in f["updates"])
    days = max(days, all_days)

    # map day -> list of fact updates landing that day
    by_day = {}
    for f in FACTS:
        for d, v in f["updates"]:
            by_day.setdefault(d, []).append((f, v))

    day_sessions = []
    for d in range(1, days + 1):
        turns = []
        for f, v in by_day.get(d, []):
            turns.append({"fact_id": f["id"], "text": f["tpl"].format(v=v),
                          "is_fact": True})
        # sprinkle 1-2 filler turns for realism (deterministic via seeded rng)
        for _ in range(rng.randint(1, 2)):
            turns.append({"fact_id": None, "text": rng.choice(FILLER),
                          "is_fact": False})
        rng.shuffle(turns)
        day_sessions.append({"day": d, "turns": turns})

    # CHECKPOINTS: probe after each day where a superseding update landed, plus the
    # final day. At each checkpoint every fact that has ≥1 update by then is probed
    # with its as-of-that-day correct answer.
    ckpts = sorted({d for f in FACTS if f["kind"] == "superseding"
                    for d, _ in f["updates"]} | {days})
    checkpoints = []
    for k in ckpts:
        probes = []
        for f in FACTS:
            v = value_asof(f, k)
            if v is None:
                continue
            probes.append({"fact_id": f["id"], "question": f["q"],
                           "answer": v, "kind": f["kind"]})
        checkpoints.append({"through_day": k, "n_probes": len(probes),
                            "probes": probes})

    return {"schema": SCHEMA_VERSION, "seed": seed, "days": days,
            "base_epoch": base_epoch, "day_sessions": day_sessions,
            "checkpoints": checkpoints,
            "n_facts": len(FACTS),
            "n_superseding": sum(1 for f in FACTS if f["kind"] == "superseding")}


# ── emit: timeline (through day K) -> lab session archive (Phase-1 shape) ──────
def emit_archive(timeline: dict, lab_dir: Path, through_day: int) -> dict:
    """Write ONE session archive containing every day's turns with day <= through_day.
    Same 3-field message shape the pipeline scans, each turn timestamped by its day
    and tagged with session_id=day<K> + fact_id so a retrieved chunk is attributable.
    Incremental: calling with a larger through_day re-emits the whole prefix (the
    runner tears down + re-emits per checkpoint to keep the lab clean)."""
    base_epoch = timeline["base_epoch"]
    sess_dir = lab_dir / "sessions"
    sess_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%dT%H-%M-%S", time.gmtime())
    out = sess_dir / f"longitudinal.through_day{through_day}.archived.{stamp}.jsonl"

    header = {"type": "session", "version": 3, "id": _hexid(),
              "timestamp": _iso(1, base_epoch), "cwd": str(lab_dir)}
    lines = [json.dumps(header)]
    prev = None
    n_msg = 0
    for ds in timeline["day_sessions"]:
        d = ds["day"]
        if d > through_day:
            break
        sid = f"day{d}"
        sdate = _iso(d, base_epoch)
        for turn in ds["turns"]:
            mid = _hexid()
            lines.append(json.dumps({
                "type": "message", "id": mid, "parentId": prev,
                "timestamp": sdate,
                "session_idx": d, "session_id": sid,
                "fact_id": turn.get("fact_id"),
                "message": {"role": "user",
                            "content": [{"type": "text", "text": turn["text"]}]},
            }))
            prev = mid
            n_msg += 1
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"path": str(out), "through_day": through_day, "messages": n_msg}


# ── gold: the probe set as-of a checkpoint (answer.py-shaped + gold sidecars) ─
def checkpoint_probes(timeline: dict, through_day: int) -> list:
    """Return the probe list whose answers are correct AS OF `through_day`.
    Matches the checkpoint schedule; if `through_day` is not an exact checkpoint,
    computes as-of values directly (so ad-hoc probe days still work)."""
    for ck in timeline["checkpoints"]:
        if ck["through_day"] == through_day:
            return ck["probes"]
    # ad-hoc: recompute as-of
    probes = []
    for f in FACTS:
        v = value_asof(f, through_day)
        if v is None:
            continue
        probes.append({"fact_id": f["id"], "question": f["q"],
                       "answer": v, "kind": f["kind"]})
    return probes


def write_gold(timeline: dict, through_day: int, lab_dir: Path,
               out_questions: Path) -> dict:
    """Write per-probe gold sidecars into <lab>/sessions AND an answer.py-shaped
    question array to `out_questions`. Question ids are checkpoint-scoped so probes
    at different checkpoints never collide (<fact>__d<K>)."""
    probes = checkpoint_probes(timeline, through_day)
    sess_dir = lab_dir / "sessions"
    sess_dir.mkdir(parents=True, exist_ok=True)
    questions = []
    for p in probes:
        qid = f"{p['fact_id']}__d{through_day}"
        gold = {
            "question_id": qid,
            "question": p["question"],
            "answer": p["answer"],
            # longitudinal facts have no session-level gold attribution the way
            # LongMemEval does; the eval axis is ACCURACY-vs-time, not retrieval
            # precision. We still tag the fact + checkpoint for per-fact curves.
            "question_type": "longitudinal-" + p["kind"],
            "fact_id": p["fact_id"],
            "through_day": through_day,
            "is_adversarial": False,
        }
        (sess_dir / f"{qid}.gold.json").write_text(
            json.dumps(gold, indent=2), encoding="utf-8")
        questions.append({"question_id": qid, "question": p["question"],
                          "answer": p["answer"],
                          "question_type": "longitudinal-" + p["kind"]})
    out_questions.write_text(json.dumps(questions, indent=2), encoding="utf-8")
    return {"through_day": through_day, "n_probes": len(probes),
            "questions": str(out_questions)}


def main() -> None:
    ap = argparse.ArgumentParser(description="Phase 2 longitudinal timeline builder")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pb = sub.add_parser("build", help="build timeline.json + checkpoints.json")
    pb.add_argument("--out", required=True, help="output dir")
    pb.add_argument("--seed", type=int, default=1729)
    pb.add_argument("--days", type=int, default=30)

    pe = sub.add_parser("emit", help="emit archive with days<=through-day into lab")
    pe.add_argument("--timeline", required=True)
    pe.add_argument("--lab", required=True)
    pe.add_argument("--through-day", type=int, required=True)
    pe.add_argument("--json", action="store_true")

    pg = sub.add_parser("gold", help="write probe gold + question file as-of checkpoint")
    pg.add_argument("--timeline", required=True)
    pg.add_argument("--lab", required=True)
    pg.add_argument("--through-day", type=int, required=True)
    pg.add_argument("--out", required=True, help="answer.py-shaped question file")

    ps = sub.add_parser("schema", help="human summary of a built timeline")
    ps.add_argument("--timeline", required=True)

    args = ap.parse_args()

    if args.cmd == "build":
        tl = build_timeline(args.seed, args.days)
        outdir = Path(args.out)
        outdir.mkdir(parents=True, exist_ok=True)
        (outdir / "timeline.json").write_text(json.dumps(tl, indent=2), encoding="utf-8")
        (outdir / "checkpoints.json").write_text(
            json.dumps({"schema": tl["schema"], "seed": tl["seed"], "days": tl["days"],
                        "checkpoints": [{"through_day": c["through_day"],
                                         "n_probes": c["n_probes"]}
                                        for c in tl["checkpoints"]]}, indent=2),
            encoding="utf-8")
        print(json.dumps({"timeline": str(outdir / "timeline.json"),
                          "days": tl["days"], "n_facts": tl["n_facts"],
                          "n_superseding": tl["n_superseding"],
                          "checkpoints": [c["through_day"] for c in tl["checkpoints"]]},
                         indent=2))
        return

    if args.cmd == "emit":
        tl = json.loads(Path(args.timeline).read_text())
        info = emit_archive(tl, Path(args.lab), args.through_day)
        print(json.dumps(info, indent=2) if args.json
              else f"emitted {info['path']} ({info['messages']} msgs, through day {info['through_day']})")
        return

    if args.cmd == "gold":
        tl = json.loads(Path(args.timeline).read_text())
        info = write_gold(tl, args.through_day, Path(args.lab), Path(args.out))
        print(json.dumps(info, indent=2))
        return

    if args.cmd == "schema":
        tl = json.loads(Path(args.timeline).read_text())
        print(f"schema={tl['schema']} seed={tl['seed']} days={tl['days']} "
              f"facts={tl['n_facts']} superseding={tl['n_superseding']}")
        for c in tl["checkpoints"]:
            print(f"  checkpoint through_day={c['through_day']:>3}  probes={c['n_probes']}")
        return


if __name__ == "__main__":
    main()
