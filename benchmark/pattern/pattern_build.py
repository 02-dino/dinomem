#!/usr/bin/env python3
"""
pattern_build.py — Phase 4 (4a): LATENT-PATTERN / RELATIONSHIP test-set builder.

WHAT PHASE 4a PROVES (and Phases 1-3 cannot):
  Phases 1-3 test facts that were STATED (retrieve / track / dedup an explicit
  fact). 4a tests facts that were NEVER stated but are ENTAILED by a chain of
  stated ones — the multi-hop / entity-graph inference the strategy PDF asks for.
  Example: "Alice manages Bob" + "Bob manages Carol" => (never said) "Alice is two
  levels above Carol" / "Carol is in Alice's org". A flat RAG store has each edge as
  a separate chunk and no way to COMPOSE them; neuron's L2 entity graph
  (memory_graph.py depends_on/affects/trace + hybrid_recall's graph leg) is the
  mechanism that can traverse the chain. So 4a = "can the engine infer the
  unstated relationship", base-vs-neuron delta shown.

  METRIC (score-side): pattern_discovery_acc = accuracy on the MULTI-HOP probe
  questions whose answer requires composing >=2 stated edges. Reported next to a
  DIRECT-fact control (single stated edge) so we separate "can't retrieve at all"
  from "can retrieve but can't compose".

HOW (reuses Phase-1 lab format UNCHANGED):
  Scripted relationship CHAINS stated as individual dated turns (each turn asserts
  ONE edge). Two probe sets:
    * direct   — answer = a single stated edge (control; every arm should pass).
    * multihop — answer = an UNSTATED node reachable only by composing >=2 edges.
  Gold sidecars carry hop_count + the edge path so the score/analysis can bucket by
  difficulty. Deterministic (seeded).

Subcommands:
  build  --out <dir> [--seed N]                         -> chains.json
  emit   --chains <c.json> --lab <labroot>              -> dated archive in lab
  gold   --chains <c.json> --lab <labroot> --out <f>    -> direct+multihop probes
  schema --chains <c.json>                              -> human summary
"""
from __future__ import annotations

import argparse
import json
import random
import time
import uuid
from pathlib import Path

SCHEMA_VERSION = "phase4-4a-2026-08-14"

def _hexid() -> str:
    return uuid.uuid4().hex[:24]

def _iso(day: int, base_epoch: int, hour: int = 12) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(base_epoch + day * 86400 + hour * 3600))

# ── relationship chains. Each CHAIN = an ordered list of (a, relation, b) edges;
# each edge is STATED as one dated turn. The multi-hop probe asks for a fact true
# only by COMPOSING >=2 edges (never stated verbatim). ──────────────────────────
CHAINS = [
    {"id": "org", "edges": [
        {"a": "Alice", "rel": "manages", "b": "Bob",   "tpl": "Alice manages Bob at work."},
        {"a": "Bob",   "rel": "manages", "b": "Carol", "tpl": "Bob manages Carol on the same team."},
        {"a": "Carol", "rel": "manages", "b": "Dan",   "tpl": "Carol manages Dan, the newest hire."},
     ],
     "direct":   {"q": "Who does Bob manage?", "answer": "Carol", "hops": 1,
                  "path": ["Bob-manages-Carol"]},
     "multihop": {"q": "Is Dan in Alice's reporting chain?", "answer": "yes", "hops": 3,
                  "path": ["Alice-manages-Bob", "Bob-manages-Carol", "Carol-manages-Dan"]},
    },
    {"id": "geo", "edges": [
        {"a": "Marco", "rel": "lives_in", "b": "Porto",    "tpl": "Marco lives in Porto."},
        {"a": "Porto", "rel": "located_in", "b": "Portugal", "tpl": "Porto is a city in Portugal."},
        {"a": "Portugal", "rel": "located_in", "b": "Europe", "tpl": "Portugal is part of Europe."},
     ],
     "direct":   {"q": "Which city does Marco live in?", "answer": "Porto", "hops": 1,
                  "path": ["Marco-lives_in-Porto"]},
     "multihop": {"q": "Which continent does Marco live in?", "answer": "Europe", "hops": 3,
                  "path": ["Marco-lives_in-Porto", "Porto-in-Portugal", "Portugal-in-Europe"]},
    },
    {"id": "supply", "edges": [
        {"a": "FactoryX", "rel": "supplies", "b": "AssemblerY", "tpl": "FactoryX supplies parts to AssemblerY."},
        {"a": "AssemblerY", "rel": "supplies", "b": "BrandZ",   "tpl": "AssemblerY supplies finished units to BrandZ."},
     ],
     "direct":   {"q": "Who does AssemblerY supply?", "answer": "BrandZ", "hops": 1,
                  "path": ["AssemblerY-supplies-BrandZ"]},
     "multihop": {"q": "Does FactoryX's output end up in BrandZ's products?", "answer": "yes", "hops": 2,
                  "path": ["FactoryX-supplies-AssemblerY", "AssemblerY-supplies-BrandZ"]},
    },
    {"id": "family", "edges": [
        {"a": "Nina", "rel": "parent_of", "b": "Omar",  "tpl": "Nina is Omar's mother."},
        {"a": "Omar", "rel": "parent_of", "b": "Priya", "tpl": "Omar is Priya's father."},
     ],
     "direct":   {"q": "Who is Omar's child?", "answer": "Priya", "hops": 1,
                  "path": ["Omar-parent_of-Priya"]},
     "multihop": {"q": "What relation is Nina to Priya?", "answer": "grandmother", "hops": 2,
                  "path": ["Nina-parent_of-Omar", "Omar-parent_of-Priya"]},
    },
    {"id": "tech", "edges": [
        {"a": "ServiceA", "rel": "depends_on", "b": "ServiceB", "tpl": "ServiceA depends on ServiceB to run."},
        {"a": "ServiceB", "rel": "depends_on", "b": "DatabaseC", "tpl": "ServiceB depends on DatabaseC for storage."},
     ],
     "direct":   {"q": "What does ServiceA depend on?", "answer": "ServiceB", "hops": 1,
                  "path": ["ServiceA-depends_on-ServiceB"]},
     "multihop": {"q": "If DatabaseC goes down, is ServiceA affected?", "answer": "yes", "hops": 2,
                  "path": ["ServiceA-depends_on-ServiceB", "ServiceB-depends_on-DatabaseC"]},
    },
]

FILLER = [
    "Had a normal day at the office.",
    "The commute was uneventful.",
    "Grabbed lunch with a colleague.",
    "Wrapped up early today.",
]

# ── DISTRACTORS (PDF §6A 'equally importantly, false pattern rate'). Each distractor
# states edges that DO NOT compose into the tempting cross-node relation. The
# false-pattern probe asks the tempting (but UNSUPPORTED) multi-hop question; the
# CORRECT answer is 'no'/'unknown'. A system that hallucinates a pattern from
# coincidence answers 'yes' -> that's a false pattern. LOWER false_pattern_rate =
# better (doesn't invent structure that was never entailed). ──────────────────────
DISTRACTORS = [
    {"id": "dx_sharedname", "edges": [
        {"a": "Alex Kim",  "rel": "works_at", "b": "Acme",  "tpl": "Alex Kim works at Acme."},
        {"a": "Alex Rossi", "rel": "works_at", "b": "Globex", "tpl": "Alex Rossi works at Globex."},
     ],
     # tempting false pattern: same first name => same person / same company. NOT entailed.
     "false_probe": {"q": "Do Alex Kim and Alex Rossi work at the same company?",
                     "answer": "no", "trap": "shared-first-name != same entity"}},
    {"id": "dx_cooccur", "edges": [
        {"a": "Sam", "rel": "attended", "b": "the Tuesday meeting", "tpl": "Sam attended the Tuesday meeting."},
        {"a": "Lee", "rel": "attended", "b": "the Tuesday meeting", "tpl": "Lee attended the Tuesday meeting."},
     ],
     # tempting false pattern: co-attendance => a reporting/managing relation. NOT entailed.
     "false_probe": {"q": "Does Sam manage Lee?",
                     "answer": "no", "trap": "co-occurrence != hierarchy"}},
    {"id": "dx_temporal", "edges": [
        {"a": "the outage", "rel": "happened_on", "b": "Monday", "tpl": "The outage happened on Monday."},
        {"a": "the deploy",  "rel": "happened_on", "b": "Monday", "tpl": "A routine deploy also happened on Monday."},
     ],
     # tempting false pattern: same-day => causation. NOT entailed.
     "false_probe": {"q": "Did the deploy cause the outage?",
                     "answer": "no", "trap": "same-day != causal"}},
]

def build_chains(seed: int) -> dict:
    edges_total = sum(len(c["edges"]) for c in CHAINS)
    dx_edges = sum(len(d["edges"]) for d in DISTRACTORS)
    return {"schema": SCHEMA_VERSION, "seed": seed, "n_chains": len(CHAINS),
            "n_edges": edges_total, "chains": CHAINS,
            "n_distractors": len(DISTRACTORS), "n_distractor_edges": dx_edges,
            "distractors": DISTRACTORS, "_filler_seed": seed}

def emit_archive(spec: dict, labroot: Path) -> dict:
    sess_dir = labroot / "sessions"
    sess_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%dT%H-%M-%S", time.gmtime())
    out = sess_dir / f"pattern.archived.{stamp}.jsonl"
    base_epoch = 1700000000
    # flatten edges to dated turns (interleave chains so no chain is contiguous —
    # forces the store to relate across time, not just adjacent chunks)
    turns = []
    day = 1
    all_groups = spec["chains"] + spec.get("distractors", [])
    maxlen = max(len(c["edges"]) for c in all_groups)
    for i in range(maxlen):
        for c in all_groups:
            if i < len(c["edges"]):
                e = c["edges"][i]
                turns.append({"day": day, "chain": c["id"], "rel": e["rel"],
                              "text": e["tpl"]})
                day += 1
    rng = random.Random(spec.get("_filler_seed", 0))
    header = {"type": "session", "version": 3, "id": _hexid(),
              "timestamp": _iso(1, base_epoch), "cwd": str(labroot)}
    lines = [json.dumps(header)]
    prev = None
    n = 0
    for t in turns:
        if rng.random() < 0.4:
            mid = _hexid()
            lines.append(json.dumps({"type": "message", "id": mid, "parentId": prev,
                "timestamp": _iso(t["day"], base_epoch, 9),
                "message": {"role": "user",
                            "content": [{"type": "text", "text": rng.choice(FILLER)}]}}))
            prev = mid; n += 1
        mid = _hexid()
        lines.append(json.dumps({"type": "message", "id": mid, "parentId": prev,
            "timestamp": _iso(t["day"], base_epoch),
            "chain": t["chain"], "relation": t["rel"],
            "message": {"role": "user", "content": [{"type": "text", "text": t["text"]}]}}))
        prev = mid; n += 1
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"path": str(out), "messages": n, "edges": spec["n_edges"]}

def write_gold(spec: dict, labroot: Path, out_questions: Path) -> dict:
    sess_dir = labroot / "sessions"
    sess_dir.mkdir(parents=True, exist_ok=True)
    questions = []
    n_direct = n_multi = 0
    for c in spec["chains"]:
        for kind in ("direct", "multihop"):
            p = c[kind]
            qid = f"{c['id']}__{kind}"
            qtype = f"pattern-{kind}"
            gold = {"question_id": qid, "question": p["q"], "answer": p["answer"],
                    "question_type": qtype, "chain": c["id"], "hops": p["hops"],
                    "edge_path": p["path"], "is_adversarial": False}
            (sess_dir / f"{qid}.gold.json").write_text(json.dumps(gold, indent=2), encoding="utf-8")
            questions.append({"question_id": qid, "question": p["q"], "answer": p["answer"],
                              "question_type": qtype, "hops": p["hops"]})
            if kind == "direct":
                n_direct += 1
            else:
                n_multi += 1
    # false-pattern probes: the tempting-but-unsupported question; correct answer = 'no'.
    n_false = 0
    for d in spec.get("distractors", []):
        fp = d["false_probe"]
        qid = f"{d['id']}__false"
        gold = {"question_id": qid, "question": fp["q"], "answer": fp["answer"],
                "question_type": "pattern-false", "distractor": d["id"], "hops": 0,
                "trap": fp["trap"], "is_adversarial": True}
        (sess_dir / f"{qid}.gold.json").write_text(json.dumps(gold, indent=2), encoding="utf-8")
        questions.append({"question_id": qid, "question": fp["q"], "answer": fp["answer"],
                          "question_type": "pattern-false", "hops": 0})
        n_false += 1
    out_questions.write_text(json.dumps(questions, indent=2), encoding="utf-8")
    return {"n_probes": len(questions), "n_direct": n_direct, "n_multihop": n_multi,
            "n_false": n_false, "questions": str(out_questions)}

def main() -> None:
    ap = argparse.ArgumentParser(description="Phase 4a latent-pattern/relationship builder")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pb = sub.add_parser("build"); pb.add_argument("--out", required=True); pb.add_argument("--seed", type=int, default=1618)
    pe = sub.add_parser("emit"); pe.add_argument("--chains", required=True); pe.add_argument("--lab", required=True); pe.add_argument("--json", action="store_true")
    pg = sub.add_parser("gold"); pg.add_argument("--chains", required=True); pg.add_argument("--lab", required=True); pg.add_argument("--out", required=True)
    ps = sub.add_parser("schema"); ps.add_argument("--chains", required=True)
    args = ap.parse_args()

    if args.cmd == "build":
        spec = build_chains(args.seed)
        outdir = Path(args.out); outdir.mkdir(parents=True, exist_ok=True)
        (outdir / "chains.json").write_text(json.dumps(spec, indent=2), encoding="utf-8")
        print(json.dumps({"chains": str(outdir / "chains.json"),
                          "n_chains": spec["n_chains"], "n_edges": spec["n_edges"]}, indent=2))
        return
    if args.cmd == "emit":
        spec = json.loads(Path(args.chains).read_text())
        info = emit_archive(spec, Path(args.lab))
        print(json.dumps(info, indent=2) if args.json
              else f"emitted {info['path']} ({info['messages']} msgs, {info['edges']} edges)")
        return
    if args.cmd == "gold":
        spec = json.loads(Path(args.chains).read_text())
        print(json.dumps(write_gold(spec, Path(args.lab), Path(args.out)), indent=2))
        return
    if args.cmd == "schema":
        spec = json.loads(Path(args.chains).read_text())
        print(f"schema={spec['schema']} seed={spec['seed']} chains={spec['n_chains']} edges={spec['n_edges']}")
        for c in spec["chains"]:
            path = " -> ".join(f"{e['a']}-{e['rel']}-{e['b']}" for e in c["edges"])
            print(f"  {c['id']:<8} {path}")
            print(f"           direct: {c['direct']['q']} = {c['direct']['answer']} ({c['direct']['hops']}h)")
            print(f"           multi : {c['multihop']['q']} = {c['multihop']['answer']} ({c['multihop']['hops']}h)")
        return

if __name__ == "__main__":
    main()
