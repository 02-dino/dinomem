#!/usr/bin/env python3
"""
dedup_build.py — Phase 3 (3b): the DEDUP / NOISE test-set builder.

WHAT PHASE 3b PROVES (and Phases 1-3a cannot):
  3a asked: when a fact is REPLACED, does the system track current + preserve history.
  3b asks the ORTHOGONAL corpus-hygiene question the strategy PDF demands: when the
  SAME fact is stated many times (duplicates) and buried in irrelevant chatter
  (noise), does the memory corpus STAY COMPACT — or does it GROW linearly with
  restatements? A memory that stores every restatement as a new item is a memory
  that will bloat, slow, and drown its own signal over a long relationship.

  METRICS (score-side, computed by dedup_run.py):
    dedup_rate            = 1 - (memory_items_kept / distinct_restatements_injected).
                            High = the corpus collapsed duplicates into few items.
    corpus_items          = raw count of distilled memory items after convergence
                            (the absolute size — lower is cleaner for equal recall).
    noise_suppression_pct = of injected pure-noise turns, fraction that did NOT
                            spawn a memory item (noise correctly dropped, not stored).
    recall_retained       = accuracy on the canonical probe questions AFTER the
                            dedup/noise stream (compaction must NOT cost recall —
                            a system that dedups by forgetting is disqualified).

  THE TENSION (why both axes are reported together): it is trivial to get a tiny
  corpus by storing nothing, and trivial to get perfect recall by storing everything.
  The real capability = SMALL corpus AND retained recall. dedup_run reports size and
  recall side by side so neither can be gamed. Naive RAG (the floor) chunks + stores
  EVERY turn -> corpus grows ~linearly with duplicates+noise, dedup_rate ~0,
  noise_suppression ~0. dinomem's dedup gate (memory_dedup_check + cleanup/review)
  is the mechanism under test.

HOW (reuses Phase-1 lab format UNCHANGED):
  A small set of CANONICAL facts, each RESTATED N times with varied phrasing across
  dated turns (true duplicates — same fact, different words), interleaved with pure
  NOISE turns (memory-irrelevant chatter that should never become a memory item).
  Gold = the canonical probe questions (answers must survive) + bookkeeping the
  score needs: n_distinct_facts, n_restatements, n_noise_turns.

Subcommands:
  build   --out <dir> [--seed N] [--restatements N] [--noise N]  -> corpus.json
  emit    --corpus <c.json> --lab <labroot>                       -> archive in lab
  gold    --corpus <c.json> --lab <labroot> --out <f>            -> probe set + manifest
  schema  --corpus <c.json>                                       -> human summary
"""
from __future__ import annotations

import argparse
import json
import random
import time
import uuid
from pathlib import Path

SCHEMA_VERSION = "phase3-3b-2026-08-14"

def _fail(msg: str) -> None:
    raise SystemExit(f"[dedup_build] FAIL: {msg}")

def _hexid() -> str:
    return uuid.uuid4().hex[:24]

def _iso(day: int, base_epoch: int, hour: int = 12) -> str:
    t = base_epoch + day * 86400 + hour * 3600
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t))

# ── canonical facts, each with several PARAPHRASES (true duplicates) ──────────
# Each fact has a stable answer + a probe question + a list of paraphrase
# templates that all state the SAME fact in different words. Restating a fact =
# emitting one of its paraphrases on a new dated turn. A dedup-capable memory
# should collapse all restatements of one fact into ~1 item; a naive store keeps
# one per restatement.
FACTS = [
    {"id": "dog_name", "q": "What is my dog's name?", "answer": "Biscuit",
     "paraphrases": [
        "My dog is called Biscuit.",
        "I have a dog named Biscuit.",
        "Biscuit, my dog, is the best.",
        "Took Biscuit (my dog) to the vet today.",
        "Just so you know, my dog's name is Biscuit.",
        "Biscuit — that's my dog — needs a walk.",
     ]},
    {"id": "home_city", "q": "Which city do I live in?", "answer": "Porto",
     "paraphrases": [
        "I live in Porto.",
        "Home for me is Porto.",
        "Based in Porto these days.",
        "My apartment in Porto is small but nice.",
        "Porto is where I live.",
        "I'm a Porto resident.",
     ]},
    {"id": "job_role", "q": "What do I do for work?", "answer": "data scientist",
     "paraphrases": [
        "I work as a data scientist.",
        "My job is data science.",
        "I'm a data scientist by profession.",
        "Day job: data scientist.",
        "I do data science for a living.",
        "Professionally, I'm a data scientist.",
     ]},
    {"id": "fav_drink", "q": "What is my favorite drink?", "answer": "green tea",
     "paraphrases": [
        "My favorite drink is green tea.",
        "I love green tea more than anything.",
        "Green tea is my go-to.",
        "Nothing beats a cup of green tea for me.",
        "I'm a green tea person.",
        "Green tea — my favorite, always.",
     ]},
    {"id": "sibling", "q": "What is my brother's name?", "answer": "Marco",
     "paraphrases": [
        "My brother's name is Marco.",
        "I have a brother called Marco.",
        "Marco is my brother.",
        "Called my brother Marco today.",
        "My sibling Marco lives abroad.",
        "Marco, my brother, is visiting soon.",
     ]},
]

# Pure NOISE: memory-irrelevant chatter. These should NEVER become a memory item.
# noise_suppression measures how many of these the pipeline correctly drops.
NOISE = [
    "The weather today is cloudy with a chance of rain.",
    "I watched an okay movie last night.",
    "Traffic was heavy on the way home.",
    "The coffee machine at work broke again.",
    "It's almost the weekend.",
    "I need to buy groceries later.",
    "My phone battery is at 30 percent.",
    "That meeting could have been an email.",
    "The elevator was slow this morning.",
    "I forgot my umbrella today.",
    "The printer is out of paper.",
    "Lunch was a bit disappointing.",
]

def build_corpus(seed: int, restatements: int, noise: int) -> dict:
    """Return the spec: for each canonical fact, `restatements` dated paraphrase
    turns (cycling through its paraphrase list); plus `noise` pure-noise turns
    interleaved by day. Deterministic given (seed, restatements, noise)."""
    rng = random.Random(seed)
    base_epoch = 1700000000
    turns = []  # (day, kind, fact_id_or_None, text)
    day = 1
    # restatements per fact
    for f in FACTS:
        for i in range(restatements):
            para = f["paraphrases"][i % len(f["paraphrases"])]
            turns.append({"day": day, "kind": "fact", "fact_id": f["id"],
                          "answer": f["answer"], "text": para})
            day += 1
    # noise turns
    for i in range(noise):
        turns.append({"day": day, "kind": "noise", "fact_id": None,
                      "text": NOISE[i % len(NOISE)]})
        day += 1
    rng.shuffle(turns)
    # re-day after shuffle so timestamps stay monotonic (order = shuffled order)
    for idx, t in enumerate(turns, start=1):
        t["day"] = idx

    n_fact_turns = sum(1 for t in turns if t["kind"] == "fact")
    n_noise_turns = sum(1 for t in turns if t["kind"] == "noise")
    return {"schema": SCHEMA_VERSION, "seed": seed, "base_epoch": base_epoch,
            "restatements_per_fact": restatements,
            "n_distinct_facts": len(FACTS),
            "n_restatements": n_fact_turns,      # total duplicate fact statements
            "n_noise_turns": n_noise_turns,
            "turns": turns,
            "facts": [{"id": f["id"], "q": f["q"], "answer": f["answer"]} for f in FACTS]}

# ── emit: corpus -> ONE dated session archive (Phase-1 shape) ─────────────────
def emit_archive(spec: dict, labroot: Path) -> dict:
    sess_dir = labroot / "sessions"
    sess_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%dT%H-%M-%S", time.gmtime())
    out = sess_dir / f"dedup.archived.{stamp}.jsonl"
    be = spec["base_epoch"]
    header = {"type": "session", "version": 3, "id": _hexid(),
              "timestamp": _iso(1, be), "cwd": str(labroot)}
    lines = [json.dumps(header)]
    prev = None
    n_msg = 0
    for t in spec["turns"]:
        mid = _hexid()
        lines.append(json.dumps({
            "type": "message", "id": mid, "parentId": prev,
            "timestamp": _iso(t["day"], be),
            "kind": t["kind"], "fact_id": t.get("fact_id"),
            "message": {"role": "user",
                        "content": [{"type": "text", "text": t["text"]}]},
        }))
        prev = mid
        n_msg += 1
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"path": str(out), "messages": n_msg,
            "restatements": spec["n_restatements"], "noise": spec["n_noise_turns"]}

# ── gold: canonical probe set + the manifest the score needs ──────────────────
def write_gold(spec: dict, labroot: Path, out_questions: Path) -> dict:
    """Write per-fact probe gold sidecars + an answer.py-shaped question file, plus
    a dedup manifest (n_distinct_facts / n_restatements / n_noise_turns) the runner
    reads to compute dedup_rate + noise_suppression."""
    sess_dir = labroot / "sessions"
    sess_dir.mkdir(parents=True, exist_ok=True)
    questions = []
    for f in spec["facts"]:
        qid = f"{f['id']}__probe"
        gold = {
            "question_id": qid,
            "question": f["q"],
            "answer": f["answer"],
            "question_type": "dedup-recall",
            "fact_id": f["id"],
            "is_adversarial": False,
        }
        (sess_dir / f"{qid}.gold.json").write_text(
            json.dumps(gold, indent=2), encoding="utf-8")
        questions.append({"question_id": qid, "question": f["q"],
                          "answer": f["answer"], "question_type": "dedup-recall"})
    out_questions.write_text(json.dumps(questions, indent=2), encoding="utf-8")
    # manifest for the score (written next to the question file)
    manifest = {
        "n_distinct_facts": spec["n_distinct_facts"],
        "n_restatements": spec["n_restatements"],
        "n_noise_turns": spec["n_noise_turns"],
        "restatements_per_fact": spec["restatements_per_fact"],
    }
    manifest_path = out_questions.with_suffix(".manifest.json")
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return {"n_probes": len(questions), "questions": str(out_questions),
            "manifest": str(manifest_path), **manifest}

def main() -> None:
    ap = argparse.ArgumentParser(description="Phase 3b dedup/noise builder")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pb = sub.add_parser("build", help="build corpus.json")
    pb.add_argument("--out", required=True)
    pb.add_argument("--seed", type=int, default=2718)
    pb.add_argument("--restatements", type=int, default=6,
                    help="times each canonical fact is restated (true duplicates)")
    pb.add_argument("--noise", type=int, default=12, help="pure-noise turns")

    pe = sub.add_parser("emit", help="emit archive into lab")
    pe.add_argument("--corpus", required=True)
    pe.add_argument("--lab", required=True)
    pe.add_argument("--json", action="store_true")

    pg = sub.add_parser("gold", help="write probe gold + manifest + question file")
    pg.add_argument("--corpus", required=True)
    pg.add_argument("--lab", required=True)
    pg.add_argument("--out", required=True)

    ps = sub.add_parser("schema", help="human summary")
    ps.add_argument("--corpus", required=True)

    args = ap.parse_args()

    if args.cmd == "build":
        spec = build_corpus(args.seed, args.restatements, args.noise)
        outdir = Path(args.out)
        outdir.mkdir(parents=True, exist_ok=True)
        (outdir / "corpus.json").write_text(json.dumps(spec, indent=2), encoding="utf-8")
        print(json.dumps({"corpus": str(outdir / "corpus.json"),
                          "n_distinct_facts": spec["n_distinct_facts"],
                          "n_restatements": spec["n_restatements"],
                          "n_noise_turns": spec["n_noise_turns"]}, indent=2))
        return

    if args.cmd == "emit":
        spec = json.loads(Path(args.corpus).read_text())
        info = emit_archive(spec, Path(args.lab))
        print(json.dumps(info, indent=2) if args.json
              else f"emitted {info['path']} ({info['messages']} msgs; "
                   f"{info['restatements']} restatements, {info['noise']} noise)")
        return

    if args.cmd == "gold":
        spec = json.loads(Path(args.corpus).read_text())
        info = write_gold(spec, Path(args.lab), Path(args.out))
        print(json.dumps(info, indent=2))
        return

    if args.cmd == "schema":
        spec = json.loads(Path(args.corpus).read_text())
        print(f"schema={spec['schema']} seed={spec['seed']} "
              f"facts={spec['n_distinct_facts']} restatements={spec['n_restatements']} "
              f"noise={spec['n_noise_turns']}")
        for f in spec["facts"]:
            print(f"  {f['id']:<12} '{f['q']}' -> {f['answer']}")
        return

if __name__ == "__main__":
    main()
