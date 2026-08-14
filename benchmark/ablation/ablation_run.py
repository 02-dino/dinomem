#!/usr/bin/env python3
"""
ablation_run.py — Phase 5 (5b): FEATURE-ABLATION orchestrator (the causal-claim engine).

WHAT PHASE 5b PROVES:
  Phases 1-4 show neuron's aggregate advantage. 5b attributes that advantage to
  SPECIFIC mechanisms by removing ONE at a time and measuring the metric it should
  govern. Remove entity-graph -> does multi-hop (4a) collapse? Remove promotion ->
  does 4b/4c collapse? Remove contradiction/synthesis -> does supersession (3a) or
  poisoning-resistance (5a) degrade? The per-mechanism DELTA vs the full-neuron
  baseline = that mechanism's CAUSAL contribution. A big drop = load-bearing; ~0 =
  the feature isn't doing the work the story claims.

MECHANISM -> (ablated neuron stage, the phase runner whose metric it governs, the
metric key to diff). Each row runs the phase's neuron arm twice: FULL baseline and
ABLATED (that stage skipped in drive_neuron via --skip-stage), then diffs the metric.

  entity_graph     memory_graph.py         pattern     multihop_acc
  synthesis        memory_synthesis.py     pattern     multihop_acc     (L3 relational)
  contradiction    contradiction_check.py  supersession supersession_correct_pct
  confidence       confidence_engine.py    promotion   promotion_precision
  promotion        memory_promote.py       promotion   promotion_recall
  topic_index      generate_topic_index.py longitudinal overall (weak signal control)

OUTPUT: results/ablation_table.json — per mechanism {baseline, ablated, delta}, the
attribution table. A NEGATIVE delta (ablated < baseline) = the mechanism HELPS.

DELEGATION: this orchestrator SHELLS the existing phase runners (supersession_run /
pattern_run / promotion_run / longitudinal_run) with --arm neuron, passing the
ablation via DINOMEM_BENCH_SKIP_STAGE (the phase runners forward it to
drive_neuron --skip-stage). It builds each phase's spec first if absent.

PAID: every row runs the neuron arm twice with a real model -> this is the most
expensive phase. Run LAST, only after the single-phase paid runs validate. Use
--only <mechanism> to run one row; --dry-run to print the plan without spending.

Usage:
  python3 ablation_run.py --source <ws> --answer-model M --judge-model M \\
      --overlay-cmd '<neuron install at {ws}>' [--only entity_graph] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
BENCH = HERE.parent
RESULTS = HERE / "results"

# mechanism -> config. runner = <phase>/<phase>_run.py; build = <phase>/<phase>_build.py
# spec_flag = the runner's spec arg name; metric = key in the runner's result JSON.
ABLATIONS = {
    "entity_graph": {
        "stage": "memory_graph.py", "phase": "pattern",
        "runner": "pattern/pattern_run.py", "build": "pattern/pattern_build.py",
        "spec_flag": "--chains", "spec_name": "chains.json", "build_out": "chains",
        "metric": "multihop_acc",
        "why": "L2 entity graph is the multi-hop compose mechanism (4a)."},
    "synthesis": {
        "stage": "memory_synthesis.py", "phase": "pattern",
        "runner": "pattern/pattern_run.py", "build": "pattern/pattern_build.py",
        "spec_flag": "--chains", "spec_name": "chains.json", "build_out": "chains",
        "metric": "multihop_acc",
        "why": "L3 synthesis stitches related items; test its multi-hop lift."},
    "contradiction": {
        "stage": "contradiction_check.py", "phase": "supersession",
        "runner": "supersession/supersession_run.py", "build": "supersession/supersession_build.py",
        "spec_flag": "--subjects", "spec_name": "subjects.json", "build_out": "subjects",
        "metric": "supersession_correct_pct",
        "why": "contradiction resolution drives current-vs-stale correctness (3a)."},
    "confidence": {
        "stage": "confidence_engine.py", "phase": "promotion",
        "runner": "promotion/promotion_run.py", "build": "promotion/promotion_build.py",
        "spec_flag": "--facts", "spec_name": "facts.json", "build_out": "facts",
        "metric": "promotion_precision",
        "why": "confidence calibration gates WHAT graduates -> promotion precision (4b)."},
    "promotion": {
        "stage": "memory_promote.py", "phase": "promotion",
        "runner": "promotion/promotion_run.py", "build": "promotion/promotion_build.py",
        "spec_flag": "--facts", "spec_name": "facts.json", "build_out": "facts",
        "metric": "promotion_recall",
        "why": "removing promotion itself should zero promotion_recall (sanity floor)."},
    "topic_index": {
        "stage": "generate_topic_index.py", "phase": "longitudinal",
        "runner": "longitudinal/longitudinal_run.py", "build": "longitudinal/longitudinal_build.py",
        "spec_flag": None, "spec_name": None, "build_out": None,
        "metric": "overall",
        "why": "weak-signal control: topic index shouldn't move core accuracy much."},
}

def _log(m): print(f"[ablation_run] {m}", file=sys.stderr)
def _fail(m):
    print(json.dumps({"ok": False, "reason": m}))
    raise SystemExit(f"[ablation_run] FAIL: {m}")

def _sh(cmd, timeout, env=None):
    return subprocess.run([str(c) for c in cmd], capture_output=True, text=True,
                          timeout=timeout, env=env)

def _json_tail(s):
    i = s.rfind("{")
    if i < 0:
        return {}
    try:
        return json.loads(s[i:])
    except Exception:  # noqa: BLE001
        return {}

def _ensure_spec(cfg, specdir, timeout):
    """Build the phase spec if absent. Returns the spec path (or None for phases
    whose runner self-builds / needs no external spec)."""
    if not cfg["spec_flag"]:
        return None
    spec_path = specdir / cfg["spec_name"]
    if spec_path.exists():
        return spec_path
    build = BENCH / cfg["build"]
    r = _sh([sys.executable, build, "build", "--out", str(specdir)], timeout)
    if r.returncode != 0:
        _fail(f"spec build failed for {cfg['phase']}: {r.stderr[-300:]}")
    if not spec_path.exists():
        # some builders name the file after build_out; fall back to a glob
        cand = list(specdir.glob("*.json"))
        if cand:
            return cand[0]
        _fail(f"spec not produced for {cfg['phase']}")
    return spec_path

def _run_phase(cfg, spec_path, args, skip_stage):
    """Shell the phase runner (neuron arm). skip_stage='' = baseline; else ablated.
    Returns the runner's result dict."""
    runner = BENCH / cfg["runner"]
    cmd = [sys.executable, runner, "--arm", "neuron", "--source", args.source]
    if spec_path is not None:
        cmd += [cfg["spec_flag"], str(spec_path)]
    if args.answer_model:
        cmd += ["--answer-model", args.answer_model]
    if args.judge_model:
        cmd += ["--judge-model", args.judge_model]
    if args.overlay_cmd:
        cmd += ["--overlay-cmd", args.overlay_cmd]
    tag = "ablated" if skip_stage else "baseline"
    out = RESULTS / f"{cfg['phase']}_{tag}_{skip_stage or 'full'}.json"
    cmd += ["--out", str(out)]
    env = dict(os.environ)
    if skip_stage:
        env["DINOMEM_BENCH_SKIP_STAGE"] = skip_stage
    r = _sh(cmd, args.timeout, env=env)
    res = _json_tail(r.stdout)
    if r.returncode != 0 or not res.get("ok"):
        _fail(f"{cfg['phase']} {tag} run failed: {res.get('reason')} :: {r.stderr[-300:]}")
    return res

def _metric(res, key):
    v = res.get(key)
    if v is None and key == "overall":
        v = res.get("overall_accuracy")
    return v

def run(args):
    RESULTS.mkdir(parents=True, exist_ok=True)
    specroot = RESULTS / "specs"; specroot.mkdir(exist_ok=True)
    rows = ({args.only: ABLATIONS[args.only]} if args.only else ABLATIONS)
    if args.only and args.only not in ABLATIONS:
        _fail(f"unknown mechanism '{args.only}'. choices: {list(ABLATIONS)}")

    if args.dry_run:
        plan = [{"mechanism": k, "ablated_stage": c["stage"], "phase": c["phase"],
                 "metric": c["metric"], "why": c["why"]} for k, c in rows.items()]
        print(json.dumps({"ok": True, "dry_run": True, "runs": 2 * len(plan),
                          "plan": plan}, indent=2))
        return

    table = []
    # baselines are cached per phase (a phase's FULL neuron run is identical across
    # its mechanisms) to avoid paying twice for the same baseline.
    baseline_cache = {}
    t0 = time.time()
    for mech, cfg in rows.items():
        specdir = specroot / cfg["phase"]; specdir.mkdir(exist_ok=True)
        spec_path = _ensure_spec(cfg, specdir, args.timeout)
        if cfg["phase"] not in baseline_cache:
            _log(f"[{mech}] baseline ({cfg['phase']} full neuron)")
            base_res = _run_phase(cfg, spec_path, args, skip_stage="")
            baseline_cache[cfg["phase"]] = base_res
        base_res = baseline_cache[cfg["phase"]]
        _log(f"[{mech}] ablate {cfg['stage']}")
        abl_res = _run_phase(cfg, spec_path, args, skip_stage=cfg["stage"])
        b = _metric(base_res, cfg["metric"]); a = _metric(abl_res, cfg["metric"])
        delta = (round(a - b, 3) if (a is not None and b is not None) else None)
        table.append({"mechanism": mech, "ablated_stage": cfg["stage"],
                      "phase": cfg["phase"], "metric": cfg["metric"],
                      "baseline": b, "ablated": a, "delta": delta,
                      "interpretation": ("load_bearing" if (delta is not None and delta < -1)
                                         else ("no_effect" if delta is not None else "n/a")),
                      "why": cfg["why"]})
        _log(f"  {mech}: {cfg['metric']} baseline={b} ablated={a} delta={delta}")

    result = {"ok": True, "schema": "phase5-5b-2026-08-14",
              "answer_model": args.answer_model or "gateway-default",
              "judge_model": args.judge_model or "gateway-default",
              "n_mechanisms": len(table), "table": table,
              "note": "delta = ablated - baseline; NEGATIVE = mechanism helps that metric. "
                      "|delta|>1 (pct/point) flagged load_bearing.",
              "seconds": round(time.time() - t0, 1)}
    out = RESULTS / "ablation_table.json"
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    result["out"] = str(out)
    print(json.dumps(result, indent=2))
    return result

def main():
    ap = argparse.ArgumentParser(description="Phase 5b feature-ablation orchestrator (causal table)")
    ap.add_argument("--source", default=os.environ.get("DINOMEM_WORKSPACE", ""))
    ap.add_argument("--answer-model", default=os.environ.get("DINOMEM_BENCH_ANSWER_MODEL", ""))
    ap.add_argument("--judge-model", default=os.environ.get("DINOMEM_BENCH_JUDGE_MODEL", ""))
    ap.add_argument("--overlay-cmd", default=os.environ.get("DINOMEM_BENCH_OVERLAY_CMD", ""))
    ap.add_argument("--only", help="run one mechanism only (key from the ablation map)")
    ap.add_argument("--dry-run", action="store_true", help="print the plan + run count, no spend")
    ap.add_argument("--timeout", type=int, default=1800)
    args = ap.parse_args()
    if not args.dry_run and not args.source:
        _fail("--source (or DINOMEM_WORKSPACE) required (unless --dry-run)")
    run(args)

if __name__ == "__main__":
    main()
