#!/usr/bin/env python3
"""
promotion_build.py — Phase 4 (4b): PROMOTION precision/recall/false-promotion builder.

WHAT PHASE 4b PROVES (neuron-only; base+RAG have no promotion tier):
  Neuron's L4 layer (memory_promote.py + promotion_state.json + confidence_engine)
  promotes SOME memory to a durable "_permanent"/trusted tier. The strategy PDF's
  question: does it promote ONLY reliable knowledge? A promoter that promotes
  everything is useless (no signal); one that promotes nothing is useless (no
  consolidation). 4b measures the promoter as a CLASSIFIER over labeled facts:

    RELIABLE facts   = stated MULTIPLE times, across turns, mutually consistent,
                       corroborated (the profile real durable knowledge has).
    UNRELIABLE facts = stated ONCE, or contradicted by a later turn, or hedged
                       ("maybe", "not sure") — the profile of noise/speculation.

  METRICS (score-side, computed by promotion_run.py from promotion_state.json):
    promotion_precision = promoted ∩ reliable / promoted        (few false promotes)
    promotion_recall    = promoted ∩ reliable / all reliable    (catches real ones)
    false_promotion_rate= promoted ∩ unreliable / promoted      (the danger metric)
  Reported together: a promoter can ace one and fail another; the capability =
  high precision AND recall with low false-promotion.

HOW: a scripted set of labeled facts. Each RELIABLE fact -> several consistent
dated restatements. Each UNRELIABLE fact -> a single mention OR a stated-then-
contradicted pair OR a hedged mention. Emitted as ONE dated archive (Phase-1 shape).
The gold manifest lists each fact's stable subject-key + reliable/unreliable label so
the runner can join promotion_state.json (which keys promotions by memory file/
subject) against the ground-truth labels. Deterministic (seeded).

NOTE: RAG + base arms have NO promotion tier -> promotion_run reports them as
"no_promotion_layer" (not a failure — it's the point: only neuron has this).

Subcommands:
  build  --out <dir> [--seed N] [--restatements N]      -> facts.json
  emit   --facts <f.json> --lab <labroot>               -> dated archive
  gold   --facts <f.json> --lab <labroot> --out <f>     -> probe + label manifest
  schema --facts <f.json>                               -> human summary
"""
from __future__ import annotations

import argparse
import json
import random
import time
import uuid
from pathlib import Path

SCHEMA_VERSION = "phase4-4b-2026-08-14"

def _hexid():
    return uuid.uuid4().hex[:24]

def _iso(day, base_epoch, hour=12):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(base_epoch + day * 86400 + hour * 3600))

# RELIABLE: repeated + consistent. UNRELIABLE: once | contradicted | hedged.
RELIABLE = [
    {"id": "allergy", "subject": "dino.allergy", "answer": "peanuts",
     "q": "What am I allergic to?",
     "statements": ["I'm allergic to peanuts.",
                    "Reminder: peanuts are a no-go for me, allergy.",
                    "Told the waiter about my peanut allergy again.",
                    "My peanut allergy is serious, carrying an EpiPen."]},
    {"id": "employer", "subject": "dino.employer", "answer": "Acme Corp",
     "q": "Who do I work for?",
     "statements": ["I work at Acme Corp.",
                    "Another long day at Acme Corp.",
                    "Acme Corp's quarterly review is next week.",
                    "Been at Acme Corp for three years now."]},
    {"id": "anniversary", "subject": "dino.anniversary", "answer": "June 12",
     "q": "When is my wedding anniversary?",
     "statements": ["Our wedding anniversary is June 12.",
                    "Planning something for June 12, our anniversary.",
                    "June 12 again — anniversary number four."]},
]

# UNRELIABLE families:
#  once       — a single mention (no corroboration)
#  contradict — asserted then reversed (should NOT promote either value)
#  hedged     — explicitly uncertain
UNRELIABLE = [
    {"id": "maybe_gym", "subject": "dino.gym", "kind": "hedged",
     "statements": ["I might join a gym, not sure yet.",
                    "Still undecided about that gym membership."]},
    {"id": "one_off_book", "subject": "dino.book", "kind": "once",
     "statements": ["I started reading some novel last night."]},
    {"id": "flip_color", "subject": "dino.favcolor", "kind": "contradict",
     "statements": ["My favorite color is blue.",
                    "Actually, scratch that — my favorite color is green now.",
                    "Hmm, maybe it's red. I keep changing my mind."]},
    {"id": "rumor_move", "subject": "dino.maybemove", "kind": "hedged",
     "statements": ["We might move to Spain someday, who knows."]},
]

FILLER = ["Nice weather today.", "Long meeting this morning.",
          "Coffee run in ten minutes.", "The wifi was flaky earlier."]

def build_facts(seed: int, restatements: int) -> dict:
    # restatements caps how many statements per reliable fact are emitted (>= its
    # own list length is fine; we cycle). Reliable facts get `restatements`, each
    # unreliable family emits its own fixed statement list.
    reliable = []
    for f in RELIABLE:
        st = [f["statements"][i % len(f["statements"])] for i in range(restatements)]
        reliable.append({**f, "emit_statements": st, "label": "reliable"})
    unreliable = [{**f, "emit_statements": list(f["statements"]), "label": "unreliable"}
                  for f in UNRELIABLE]
    return {"schema": SCHEMA_VERSION, "seed": seed, "restatements_per_reliable": restatements,
            "n_reliable": len(reliable), "n_unreliable": len(unreliable),
            "reliable": reliable, "unreliable": unreliable, "_filler_seed": seed}

def emit_archive(spec: dict, labroot: Path) -> dict:
    sess_dir = labroot / "sessions"; sess_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%dT%H-%M-%S", time.gmtime())
    out = sess_dir / f"promotion.archived.{stamp}.jsonl"
    base_epoch = 1700000000
    turns = []
    # interleave all statements across facts by round so reliable restatements are
    # SPREAD across the timeline (corroboration over time, as in real use)
    pools = [(f["subject"], f["label"], list(f["emit_statements"]))
             for f in spec["reliable"] + spec["unreliable"]]
    round_i = 0
    while any(p[2] for p in pools):
        for subj, label, st in pools:
            if st:
                turns.append({"subject": subj, "label": label, "text": st.pop(0)})
        round_i += 1
    rng = random.Random(spec.get("_filler_seed", 0))
    header = {"type": "session", "version": 3, "id": _hexid(),
              "timestamp": _iso(1, base_epoch), "cwd": str(labroot)}
    lines = [json.dumps(header)]; prev = None; day = 1; n = 0
    for t in turns:
        if rng.random() < 0.35:
            mid = _hexid()
            lines.append(json.dumps({"type": "message", "id": mid, "parentId": prev,
                "timestamp": _iso(day, base_epoch, 9),
                "message": {"role": "user", "content": [{"type": "text", "text": rng.choice(FILLER)}]}}))
            prev = mid; n += 1
        mid = _hexid()
        lines.append(json.dumps({"type": "message", "id": mid, "parentId": prev,
            "timestamp": _iso(day, base_epoch), "subject": t["subject"], "reliability": t["label"],
            "message": {"role": "user", "content": [{"type": "text", "text": t["text"]}]}}))
        prev = mid; day += 1; n += 1
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"path": str(out), "messages": n,
            "reliable": spec["n_reliable"], "unreliable": spec["n_unreliable"]}

def write_gold(spec: dict, labroot: Path, out_questions: Path) -> dict:
    """Probe questions = the RELIABLE facts' recall questions (a promoted fact must
    stay retrievable). Plus a label manifest the runner joins with promotion_state."""
    sess_dir = labroot / "sessions"; sess_dir.mkdir(parents=True, exist_ok=True)
    questions = []
    for f in spec["reliable"]:
        qid = f"{f['id']}__recall"
        gold = {"question_id": qid, "question": f["q"], "answer": f["answer"],
                "question_type": "promotion-recall", "subject": f["subject"],
                "is_adversarial": False}
        (sess_dir / f"{qid}.gold.json").write_text(json.dumps(gold, indent=2), encoding="utf-8")
        questions.append({"question_id": qid, "question": f["q"], "answer": f["answer"],
                          "question_type": "promotion-recall"})
    out_questions.write_text(json.dumps(questions, indent=2), encoding="utf-8")
    manifest = {
        "reliable_subjects": [f["subject"] for f in spec["reliable"]],
        "unreliable_subjects": [f["subject"] for f in spec["unreliable"]],
        "n_reliable": spec["n_reliable"], "n_unreliable": spec["n_unreliable"],
    }
    mpath = out_questions.with_suffix(".manifest.json")
    mpath.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return {"n_probes": len(questions), "questions": str(out_questions),
            "manifest": str(mpath), **manifest}

def main():
    ap = argparse.ArgumentParser(description="Phase 4b promotion precision/recall builder")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pb = sub.add_parser("build"); pb.add_argument("--out", required=True); pb.add_argument("--seed", type=int, default=2024); pb.add_argument("--restatements", type=int, default=4)
    pe = sub.add_parser("emit"); pe.add_argument("--facts", required=True); pe.add_argument("--lab", required=True); pe.add_argument("--json", action="store_true")
    pg = sub.add_parser("gold"); pg.add_argument("--facts", required=True); pg.add_argument("--lab", required=True); pg.add_argument("--out", required=True)
    ps = sub.add_parser("schema"); ps.add_argument("--facts", required=True)
    args = ap.parse_args()

    if args.cmd == "build":
        spec = build_facts(args.seed, args.restatements)
        outdir = Path(args.out); outdir.mkdir(parents=True, exist_ok=True)
        (outdir / "facts.json").write_text(json.dumps(spec, indent=2), encoding="utf-8")
        print(json.dumps({"facts": str(outdir / "facts.json"),
                          "n_reliable": spec["n_reliable"], "n_unreliable": spec["n_unreliable"]}, indent=2))
        return
    if args.cmd == "emit":
        spec = json.loads(Path(args.facts).read_text())
        info = emit_archive(spec, Path(args.lab))
        print(json.dumps(info, indent=2) if args.json
              else f"emitted {info['path']} ({info['messages']} msgs; {info['reliable']} reliable, {info['unreliable']} unreliable)")
        return
    if args.cmd == "gold":
        spec = json.loads(Path(args.facts).read_text())
        print(json.dumps(write_gold(spec, Path(args.lab), Path(args.out)), indent=2))
        return
    if args.cmd == "schema":
        spec = json.loads(Path(args.facts).read_text())
        print(f"schema={spec['schema']} seed={spec['seed']} reliable={spec['n_reliable']} unreliable={spec['n_unreliable']}")
        for f in spec["reliable"]:
            print(f"  RELIABLE   {f['subject']:<18} x{len(f['emit_statements'])} -> {f['answer']}")
        for f in spec["unreliable"]:
            print(f"  unreliable {f['subject']:<18} [{f['kind']}] x{len(f['emit_statements'])}")
        return

if __name__ == "__main__":
    main()
