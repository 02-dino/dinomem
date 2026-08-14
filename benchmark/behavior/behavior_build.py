#!/usr/bin/env python3
"""
behavior_build.py — Phase 4 (4c): BEHAVIOR-CHANGE test-set builder.

WHAT PHASE 4c PROVES (the ultimate memory question):
  4b showed neuron promotes ONLY reliable knowledge. 4c asks: does that promoted
  knowledge actually CHANGE future responses? Memory that stores-and-retrieves but
  never alters behavior is inert. The test: a reliable CONSTRAINT is stated across
  the haystack (e.g. "I'm allergic to peanuts"); later a downstream request
  ("suggest a snack for me") has a RIGHT answer that must RESPECT the constraint
  (no peanuts) and a WRONG answer that ignores it. We answer each held-out probe
  TWICE — promotion/memory ACTIVE vs SUPPRESSED — and measure how often the active
  condition changes the response to honor the remembered constraint.

  METRIC (score-side, computed by behavior_run.py):
    behavior_change_rate = of held-out probes, fraction where the MEMORY-ON answer
                           satisfies the constraint AND the MEMORY-OFF answer does
                           not (memory demonstrably changed the response).
    on_constraint_pct    = fraction of ON answers that satisfy the constraint.
    off_constraint_pct   = fraction of OFF answers that satisfy it (baseline leakage
                           — some constraints are guessable without memory; the
                           DELTA on->off is the causal memory effect).

  This is an A/B on the RECALL condition, not the model: same model, same prompt,
  same probe — only whether the remembered constraint is fed. So the delta is
  attributable to memory, not to the answerer.

HAYSTACK: each SCENARIO states a durable constraint (repeated, reliable) + filler.
Held-out PROBE per scenario = a request whose correct fulfillment requires the
constraint. Gold carries: the constraint keyword(s) that a constraint-satisfying
answer must include OR must exclude (satisfy = includes required / excludes
forbidden), so scoring is deterministic keyword-check (no judge needed for the
constraint axis; recall judge optional).

Subcommands:
  build  --out <dir> [--seed N] [--restatements N]     -> scenarios.json
  emit   --scenarios <s.json> --lab <labroot>          -> dated archive
  gold   --scenarios <s.json> --lab <labroot> --out <f>-> held-out probe set + rubric
  schema --scenarios <s.json>                          -> human summary
"""
from __future__ import annotations

import argparse
import json
import random
import time
import uuid
from pathlib import Path

SCHEMA_VERSION = "phase4-4c-2026-08-14"

def _hexid():
    return uuid.uuid4().hex[:24]

def _iso(day, base_epoch, hour=12):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(base_epoch + day * 86400 + hour * 3600))

# Each scenario: a durable CONSTRAINT (stated reliably) + a downstream PROBE whose
# correct answer must satisfy the constraint. rubric: forbidden (answer must NOT
# contain) and/or required (answer MUST contain) keywords.
SCENARIOS = [
    {"id": "allergy",
     "constraint": "I'm allergic to peanuts.",
     "restate": ["Peanuts make me sick — serious allergy.",
                 "Remember, no peanuts for me, ever."],
     "probe": "Suggest a quick snack for me.",
     "rubric": {"forbidden": ["peanut"], "required": []},
     "note": "a memory-aware answer avoids peanuts; memory-off may suggest them."},
    {"id": "vegetarian",
     "constraint": "I'm vegetarian.",
     "restate": ["I don't eat meat — vegetarian for years.",
                 "Keep it vegetarian for me, please."],
     "probe": "Recommend a dinner recipe for tonight.",
     "rubric": {"forbidden": ["chicken", "beef", "pork", "meat", "fish"], "required": []},
     "note": "memory-aware answer stays vegetarian."},
    {"id": "budget",
     "constraint": "My budget cap for gadgets is 200 dollars.",
     "restate": ["Don't suggest anything over 200 dollars for gadgets.",
                 "200 is my hard limit on tech purchases."],
     "probe": "Recommend a new pair of headphones.",
     "rubric": {"forbidden": [], "required": ["200", "budget", "under", "affordable", "cheap"],
                "required_mode": "any"},
     "note": "memory-aware answer respects the 200-dollar cap / mentions budget."},
    {"id": "language",
     "constraint": "Please always reply to me in Spanish.",
     "restate": ["Reminder: I prefer all answers in Spanish.",
                 "Spanish only, por favor."],
     "probe": "Give me a short greeting.",
     "rubric": {"forbidden": [], "required": ["hola", "buenos", "buenas", "saludos"],
                "required_mode": "any"},
     "note": "memory-aware answer greets in Spanish."},
    {"id": "name",
     "constraint": "Call me Captain, it's my nickname.",
     "restate": ["Everyone calls me Captain.",
                 "I go by Captain, not my real name."],
     "probe": "Write me a one-line motivational note.",
     "rubric": {"forbidden": [], "required": ["captain"], "required_mode": "any"},
     "note": "memory-aware answer addresses the user as Captain."},
]

FILLER = ["The day went by fast.", "Had a decent lunch.",
          "Nothing much happening.", "Caught up on email."]

def build_scenarios(seed: int, restatements: int) -> dict:
    out = []
    for s in SCENARIOS:
        stmts = [s["constraint"]] + [s["restate"][i % len(s["restate"])]
                                     for i in range(max(0, restatements - 1))]
        out.append({**s, "emit_statements": stmts})
    return {"schema": SCHEMA_VERSION, "seed": seed, "restatements": restatements,
            "n_scenarios": len(out), "scenarios": out, "_filler_seed": seed}

def emit_archive(spec: dict, labroot: Path) -> dict:
    sess_dir = labroot / "sessions"; sess_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%dT%H-%M-%S", time.gmtime())
    out = sess_dir / f"behavior.archived.{stamp}.jsonl"
    base_epoch = 1700000000
    pools = [(s["id"], list(s["emit_statements"])) for s in spec["scenarios"]]
    turns = []
    while any(p[1] for p in pools):
        for sid, st in pools:
            if st:
                turns.append({"sid": sid, "text": st.pop(0)})
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
            "timestamp": _iso(day, base_epoch), "scenario": t["sid"],
            "message": {"role": "user", "content": [{"type": "text", "text": t["text"]}]}}))
        prev = mid; day += 1; n += 1
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"path": str(out), "messages": n, "scenarios": spec["n_scenarios"]}

def write_gold(spec: dict, labroot: Path, out_questions: Path) -> dict:
    """Held-out probes (one per scenario) + the constraint rubric the runner uses to
    deterministically check whether an answer SATISFIES the remembered constraint."""
    sess_dir = labroot / "sessions"; sess_dir.mkdir(parents=True, exist_ok=True)
    questions = []
    for s in spec["scenarios"]:
        qid = f"{s['id']}__probe"
        gold = {"question_id": qid, "question": s["probe"],
                "question_type": "behavior-probe", "scenario": s["id"],
                "rubric": s["rubric"], "constraint": s["constraint"],
                # answer field carries the constraint description (for any judge fallback)
                "answer": f"an answer that respects: {s['constraint']}",
                "is_adversarial": False}
        (sess_dir / f"{qid}.gold.json").write_text(json.dumps(gold, indent=2), encoding="utf-8")
        questions.append({"question_id": qid, "question": s["probe"],
                          "question_type": "behavior-probe", "scenario": s["id"],
                          "rubric": s["rubric"]})
    out_questions.write_text(json.dumps(questions, indent=2), encoding="utf-8")
    return {"n_probes": len(questions), "questions": str(out_questions)}

def main():
    ap = argparse.ArgumentParser(description="Phase 4c behavior-change builder")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pb = sub.add_parser("build"); pb.add_argument("--out", required=True); pb.add_argument("--seed", type=int, default=4242); pb.add_argument("--restatements", type=int, default=3)
    pe = sub.add_parser("emit"); pe.add_argument("--scenarios", required=True); pe.add_argument("--lab", required=True); pe.add_argument("--json", action="store_true")
    pg = sub.add_parser("gold"); pg.add_argument("--scenarios", required=True); pg.add_argument("--lab", required=True); pg.add_argument("--out", required=True)
    ps = sub.add_parser("schema"); ps.add_argument("--scenarios", required=True)
    args = ap.parse_args()

    if args.cmd == "build":
        spec = build_scenarios(args.seed, args.restatements)
        outdir = Path(args.out); outdir.mkdir(parents=True, exist_ok=True)
        (outdir / "scenarios.json").write_text(json.dumps(spec, indent=2), encoding="utf-8")
        print(json.dumps({"scenarios": str(outdir / "scenarios.json"), "n_scenarios": spec["n_scenarios"]}, indent=2))
        return
    if args.cmd == "emit":
        spec = json.loads(Path(args.scenarios).read_text())
        info = emit_archive(spec, Path(args.lab))
        print(json.dumps(info, indent=2) if args.json
              else f"emitted {info['path']} ({info['messages']} msgs, {info['scenarios']} scenarios)")
        return
    if args.cmd == "gold":
        spec = json.loads(Path(args.scenarios).read_text())
        print(json.dumps(write_gold(spec, Path(args.lab), Path(args.out)), indent=2))
        return
    if args.cmd == "schema":
        spec = json.loads(Path(args.scenarios).read_text())
        print(f"schema={spec['schema']} seed={spec['seed']} scenarios={spec['n_scenarios']}")
        for s in spec["scenarios"]:
            r = s["rubric"]
            rub = f"forbid={r.get('forbidden')} req={r.get('required')}({r.get('required_mode','all')})"
            print(f"  {s['id']:<11} constraint='{s['constraint']}'")
            print(f"              probe='{s['probe']}' | {rub}")
        return

if __name__ == "__main__":
    main()
