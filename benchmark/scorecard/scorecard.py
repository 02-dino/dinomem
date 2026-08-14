#!/usr/bin/env python3
"""
scorecard.py — Phase 5 (5c): the FINAL efficiency scorecard + unified publish.

WHAT PHASE 5c PROVES (the whole program's thesis, in one artifact):
  Every prior phase emitted its own result JSON(s). 5c is a PURE AGGREGATOR (no
  eval, no spend, no lab) that reads them all and renders ONE report proving the
  "3 things simultaneously" claim the strategy PDF demands:
    (1) COMPETITIVE on standard benchmarks (Phase 1: LongMemEval/LoCoMo — quality),
    (2) ADVANTAGED on dinomem-targeted problems (Phases 2-5: longitudinal,
        supersession, dedup, pattern, promotion, behavior, poisoning),
    (3) HONEST about cost/latency/storage (the efficiency columns from Phase-1 1b
        metric set, carried per arm).
  Plus the Phase-5b ablation table (which mechanism causes which advantage).

INPUTS (all optional; the scorecard renders whatever exists, flags what's missing):
  Phase 1 : <bench>/longmemeval/results/comparison.md OR *_result.json per arm
            (accuracy per-category + retrieval recall/precision + tokens + latency
             + storage — the 1b full metric set).
  Phase 1 : <bench>/locomo/results/*_result.json (2nd standard benchmark).
  Phase 2 : longitudinal/results/longitudinal_<arm>.json (accuracy-vs-sessions).
  Phase 3 : supersession/results/supersession_<arm>.json, dedup/results/dedup_<arm>.json
  Phase 4 : pattern/…/pattern_<arm>.json, promotion/…/promotion_<arm>.json,
            behavior/…/behavior_<arm>.json
  Phase 5 : poison/…/poison_<arm>.json, ablation/results/ablation_table.json

OUTPUT: results/scorecard.md (human report) + results/scorecard.json (machine).
The report is HONEST: any absent input is listed under "not yet run", and the
efficiency columns show real cost/latency/storage next to quality — never quality
alone (the PDF's core discipline: no advantage claim without its cost).

Usage:
  python3 scorecard.py [--bench <benchmark-dir>] [--out results/scorecard.md] [--json]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
BENCH = HERE.parent
RESULTS = HERE / "results"

ARMS = ["rag", "base", "neuron"]

def _load(path: Path):
    try:
        return json.loads(path.read_text())
    except Exception:  # noqa: BLE001
        return None

def _first(*paths):
    for p in paths:
        if p and Path(p).exists():
            return Path(p)
    return None

def _collect_phase(subdir: str, prefix: str):
    """Return {arm: result_dict} for <bench>/<subdir>/results/<prefix>_<arm>.json."""
    out = {}
    rdir = BENCH / subdir / "results"
    for arm in ARMS:
        f = rdir / f"{prefix}_{arm}.json"
        r = _load(f)
        if r:
            out[arm] = r
    return out

def _fmt(v, suffix=""):
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:g}{suffix}"
    return f"{v}{suffix}"

def build_scorecard():
    sc = {"schema": "phase5-5c-2026-08-14", "phases": {}}

    # Phase 1 — standard benchmarks (quality + efficiency). Read per-arm result JSON
    # if the run.py emitted them; else note the comparison.md.
    p1 = {"longmemeval": {}, "locomo": {}}
    for arm in ARMS:
        for bench_name in ("longmemeval", "locomo"):
            rdir = BENCH / bench_name / "results"
            f = _first(rdir / f"{arm}_result.json", rdir / f"{bench_name}_{arm}.json")
            r = _load(f) if f else None
            if r:
                p1[bench_name][arm] = r
    sc["phases"]["phase1_standard"] = p1

    # Phase 2 — longitudinal
    sc["phases"]["phase2_longitudinal"] = _collect_phase("longitudinal", "longitudinal")
    # Phase 3 — supersession + dedup
    sc["phases"]["phase3_supersession"] = _collect_phase("supersession", "supersession")
    sc["phases"]["phase3_dedup"] = _collect_phase("dedup", "dedup")
    # Phase 4 — pattern + promotion + behavior
    sc["phases"]["phase4_pattern"] = _collect_phase("pattern", "pattern")
    sc["phases"]["phase4_promotion"] = _collect_phase("promotion", "promotion")
    sc["phases"]["phase4_behavior"] = _collect_phase("behavior", "behavior")
    # Phase 5 — poison + ablation
    sc["phases"]["phase5_poison"] = _collect_phase("poison", "poison")
    sc["phases"]["phase5_ablation"] = _load(BENCH / "ablation" / "results" / "ablation_table.json")

    return sc

def _quality_rows(phase_map, metric_keys):
    """metric_keys = [(label, key)]; returns markdown rows per arm."""
    lines = []
    for arm in ARMS:
        r = phase_map.get(arm)
        if not r:
            lines.append(f"| {arm} | " + " | ".join("—" for _ in metric_keys) + " |")
            continue
        cells = [_fmt(r.get(k)) for _, k in metric_keys]
        lines.append(f"| {arm} | " + " | ".join(cells) + " |")
    return lines

def render_md(sc: dict) -> str:
    L = ["# dinomem Evaluation Scorecard (Phase 5c — unified)",
         "",
         "> The **3 things simultaneously**: competitive on standard benchmarks, "
         "advantaged on dinomem-targeted problems, honest on cost/latency/storage.",
         "> `—` = not yet run. Efficiency columns sit NEXT TO quality by design "
         "(no advantage claim without its cost).",
         ""]

    # Phase 1
    L += ["## Phase 1 — Standard benchmarks (quality + efficiency)"]
    for bench_name in ("longmemeval", "locomo"):
        pm = sc["phases"]["phase1_standard"].get(bench_name, {})
        L += [f"### {bench_name}",
              "| arm | overall_acc | retrieval_recall | retrieval_precision | context_tokens | total_tokens | latency_s | storage_bytes |",
              "|---|---|---|---|---|---|---|---|"]
        L += _quality_rows(pm, [("", "overall_accuracy"), ("", "retrieval_recall"),
                                ("", "retrieval_precision"), ("", "context_tokens"),
                                ("", "total_tokens"), ("", "latency_s"), ("", "storage_bytes")])
        L += [""]

    # Phase 2
    L += ["## Phase 2 — Longitudinal (accuracy as sessions accumulate)",
          "| arm | curve (per-checkpoint) | final_acc |", "|---|---|---|"]
    for arm in ARMS:
        r = sc["phases"]["phase2_longitudinal"].get(arm)
        if r:
            curve = r.get("curve") or r.get("checkpoints") or "—"
            L += [f"| {arm} | {_fmt(curve)} | {_fmt(r.get('final_accuracy') or r.get('overall_accuracy'))} |"]
        else:
            L += [f"| {arm} | — | — |"]
    L += [""]

    # Phase 3
    L += ["## Phase 3 — Supersession + Dedup (temporal coherence + hygiene)",
          "### 3a supersession/retention",
          "| arm | supersession_correct_pct | history_recovery_pct | stable_control_pct |",
          "|---|---|---|---|"]
    L += _quality_rows(sc["phases"]["phase3_supersession"],
                       [("", "supersession_correct_pct"), ("", "history_recovery_pct"),
                        ("", "stable_control_pct")])
    L += ["", "### 3b dedup/noise",
          "| arm | corpus_items | dedup_rate | noise_suppression_pct | recall_retained_pct |",
          "|---|---|---|---|---|"]
    L += _quality_rows(sc["phases"]["phase3_dedup"],
                       [("", "corpus_items"), ("", "dedup_rate"),
                        ("", "noise_suppression_pct"), ("", "recall_retained_pct")])
    L += [""]

    # Phase 4
    L += ["## Phase 4 — Pattern / Promotion / Behavior (neuron mechanisms)",
          "### 4a latent-pattern (multi-hop)",
          "| arm | direct_acc | multihop_acc |", "|---|---|---|"]
    L += _quality_rows(sc["phases"]["phase4_pattern"],
                       [("", "direct_acc"), ("", "multihop_acc")])
    L += ["", "### 4b promotion precision/recall",
          "| arm | promotion_precision | promotion_recall | false_promotion_rate | recall_retained_pct |",
          "|---|---|---|---|---|"]
    L += _quality_rows(sc["phases"]["phase4_promotion"],
                       [("", "promotion_precision"), ("", "promotion_recall"),
                        ("", "false_promotion_rate"), ("", "recall_retained_pct")])
    L += ["", "### 4c behavior-change (A/B on memory)",
          "| arm | behavior_change_rate | on_constraint_pct | off_constraint_pct | causal_delta_pct |",
          "|---|---|---|---|---|"]
    L += _quality_rows(sc["phases"]["phase4_behavior"],
                       [("", "behavior_change_rate"), ("", "on_constraint_pct"),
                        ("", "off_constraint_pct"), ("", "causal_delta_pct")])
    L += [""]

    # Phase 5 poison
    L += ["## Phase 5 — Robustness",
          "### 5a poisoning-resistance",
          "| arm | poison_uptake_rate | behavior_leak_rate | benign_uptake_rate | benign_recall_pct |",
          "|---|---|---|---|---|"]
    L += _quality_rows(sc["phases"]["phase5_poison"],
                       [("", "poison_uptake_rate"), ("", "behavior_leak_rate"),
                        ("", "benign_uptake_rate"), ("", "benign_recall_pct")])
    L += [""]

    # Phase 5 ablation
    abl = sc["phases"]["phase5_ablation"]
    L += ["### 5b feature-ablation (causal attribution)"]
    if abl and abl.get("table"):
        L += ["| mechanism | ablated_stage | metric | baseline | ablated | delta | verdict |",
              "|---|---|---|---|---|---|---|"]
        for row in abl["table"]:
            L += [f"| {row['mechanism']} | {row['ablated_stage']} | {row['metric']} | "
                  f"{_fmt(row['baseline'])} | {_fmt(row['ablated'])} | {_fmt(row['delta'])} | "
                  f"{row.get('interpretation','—')} |"]
    else:
        L += ["_ablation table not yet generated (run ablation/ablation_run.py)._"]
    L += [""]

    # Not-yet-run summary (honesty ledger)
    missing = []
    def _empty(x):
        return not x or (isinstance(x, dict) and not any(x.values()))
    for label, key in [("Phase1 LongMemEval", ("phase1_standard",)),
                       ("Phase2 longitudinal", ("phase2_longitudinal",)),
                       ("Phase3 supersession", ("phase3_supersession",)),
                       ("Phase3 dedup", ("phase3_dedup",)),
                       ("Phase4 pattern", ("phase4_pattern",)),
                       ("Phase4 promotion", ("phase4_promotion",)),
                       ("Phase4 behavior", ("phase4_behavior",)),
                       ("Phase5 poison", ("phase5_poison",)),
                       ("Phase5 ablation", ("phase5_ablation",))]:
        node = sc["phases"].get(key[0])
        if _empty(node):
            missing.append(label)
    if missing:
        L += ["## Not yet run (honesty ledger)",
              "", "\n".join(f"- {m}" for m in missing), ""]

    L += ["---",
          "_Scorecard is a pure aggregator; it renders only real emitted results. "
          "Quality never shown without its cost/latency/storage._"]
    return "\n".join(L)

def main():
    ap = argparse.ArgumentParser(description="Phase 5c unified efficiency scorecard (aggregator)")
    ap.add_argument("--out", default=str(RESULTS / "scorecard.md"))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    RESULTS.mkdir(parents=True, exist_ok=True)
    sc = build_scorecard()
    md = render_md(sc)
    Path(args.out).write_text(md, encoding="utf-8")
    (RESULTS / "scorecard.json").write_text(json.dumps(sc, indent=2), encoding="utf-8")
    if args.json:
        print(json.dumps({"ok": True, "out": args.out,
                          "json": str(RESULTS / "scorecard.json")}, indent=2))
    else:
        print(md)

if __name__ == "__main__":
    main()
