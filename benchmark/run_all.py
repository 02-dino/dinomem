#!/usr/bin/env python3
"""
run_all.py — ONE command to run the ENTIRE dinomem evaluation program.

People who want to evaluate a memory system should not have to know the phase
order, spec-build step, or per-runner flags. This is the single front door:
it builds every phase's spec, runs each phase in dependency order for the arms
you ask for, then renders the unified scorecard. One command, one config.

    python3 run_all.py --source <your-workspace> \
        --answer-model gpt-4o-mini --judge-model gpt-4o \
        --overlay-cmd 'bash <neuron>/scripts/install.sh --workspace {ws} --agent-id lab --agree --no-cron --no-auto-base'

PREVIEW FIRST (no spend, always safe):
    python3 run_all.py --source <ws> --dry-run        # print the full plan + run count
    python3 run_all.py --source <ws> --estimate-only  # Phase-1 cost estimate per arm, no run

DEFAULT ARMS = rag,base,neuron (the full comparison). Restrict with --arms rag
(the only $0 arm — base/neuron need a real --answer-model; see NOTE below).
DEFAULT PHASES = all. Restrict with --phases 1,3a,5b etc.

RUN ORDER (dependency-correct, cheap-first, ablation last):
    1   standard   LongMemEval sample        (+ LoCoMo if --locomo)   [PAID for base/neuron]
    2   longitudinal accuracy-vs-sessions
    3a  supersession current-vs-stale
    3b  dedup       corpus hygiene
    4a  pattern     multi-hop inference
    4b  promotion   precision/recall         [neuron-only metric]
    4c  behavior    A/B behavior-change
    5a  poison      poisoning resistance
    5b  ablation    causal attribution       [most expensive: 2 neuron runs x 6 mechanisms]
    ->  scorecard   unified report (pure aggregator, no spend)

NOTE — what is free vs paid:
  * The RAG arm runs at $0 (pure embedding retrieval, no LLM extraction).
  * base/neuron arms REQUIRE a real --answer-model: their memory is built by an
    LLM extraction pipeline; with no model / --stub-judge they materialize zero
    memory items and the drive fails by design. So a real neuron-vs-floor result
    costs money. --dry-run / --estimate-only never spend.

The scorecard renders whatever completed and lists the rest under a not-yet-run
honesty ledger, so a partial run still produces a valid report.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Phase registry. Each entry is dependency-ordered. Fields:
#   id/title/dir       — identity + where results land
#   build              — (script, [args]) to produce the spec (None = runner self-builds)
#   spec_flag/spec     — how the runner receives the spec
#   runner             — the *_run.py (relative to benchmark/)
#   arms               — which arms this phase runs (some are neuron-only)
#   paid_for           — arms that cost money here (rag is free везде; base/neuron paid)
PHASES = [
    {"id": "1", "title": "standard (LongMemEval)", "dir": "longmemeval",
     "runner": "longmemeval/run.py", "build": None, "spec_flag": None, "spec": None,
     "arms": ["rag", "base", "neuron"], "extra": ["--sample", "--yes"],
     "estimatable": True},
    {"id": "2", "title": "longitudinal", "dir": "longitudinal",
     "runner": "longitudinal/longitudinal_run.py",
     "build": ("longitudinal/longitudinal_build.py", "timeline.json"),
     "spec_flag": "--timeline", "spec": "timeline.json",
     "arms": ["rag", "base", "neuron"]},
    {"id": "3a", "title": "supersession", "dir": "supersession",
     "runner": "supersession/supersession_run.py",
     "build": ("supersession/supersession_build.py", "subjects.json"),
     "spec_flag": "--subjects", "spec": "subjects.json",
     "arms": ["rag", "base", "neuron"]},
    {"id": "3b", "title": "dedup", "dir": "dedup",
     "runner": "dedup/dedup_run.py",
     "build": ("dedup/dedup_build.py", "corpus.json"),
     "spec_flag": "--corpus", "spec": "corpus.json",
     "arms": ["rag", "base", "neuron"]},
    {"id": "4a", "title": "pattern (multi-hop)", "dir": "pattern",
     "runner": "pattern/pattern_run.py",
     "build": ("pattern/pattern_build.py", "chains.json"),
     "spec_flag": "--chains", "spec": "chains.json",
     "arms": ["rag", "base", "neuron"]},
    {"id": "4b", "title": "promotion", "dir": "promotion",
     "runner": "promotion/promotion_run.py",
     "build": ("promotion/promotion_build.py", "facts.json"),
     "spec_flag": "--facts", "spec": "facts.json",
     "arms": ["neuron"]},  # promotion is a neuron-only L4 mechanism
    {"id": "4c", "title": "behavior (A/B)", "dir": "behavior",
     "runner": "behavior/behavior_run.py",
     "build": ("behavior/behavior_build.py", "scenarios.json"),
     "spec_flag": "--scenarios", "spec": "scenarios.json",
     "arms": ["base", "neuron"]},  # A/B needs a memory arm; rag has no ON/OFF distinction here
    {"id": "5a", "title": "poison (resistance)", "dir": "poison",
     "runner": "poison/poison_run.py",
     "build": ("poison/poison_build.py", "poison.json"),
     "spec_flag": "--poison", "spec": "poison.json",
     "arms": ["rag", "base", "neuron"]},
    {"id": "5b", "title": "ablation (causal)", "dir": "ablation",
     "runner": "ablation/ablation_run.py", "build": None,
     "spec_flag": None, "spec": None, "arms": ["neuron"],  # self-orchestrates neuron arm
     "is_ablation": True},
    # ── direct-call safety/capability phases (FREE: pure-python gate/mechanic checks,
    # no LLM answer step). They import the arm's procedure from --source and grade it.
    {"id": "5c", "title": "authority (untrusted-instruction gate)", "dir": "authority",
     "runner": "authority/authority_run.py",
     "build": ("authority/authority_build.py", "cases.json"),
     "spec_flag": "--cases", "spec": "cases.json",
     "arms": ["rag", "base", "neuron"], "direct_call": True},  # mem_authority (base-tier)
    {"id": "5d", "title": "recovery (reversible cleanup)", "dir": "recovery",
     "runner": "recovery/recovery_run.py", "build": None,
     "spec_flag": None, "spec": None,
     "arms": ["rag", "base", "neuron"], "direct_call": True},  # _memory_diff (base-tier)
    {"id": "5e", "title": "entity resolution", "dir": "entityres",
     "runner": "entityres/entityres_run.py", "build": None,
     "spec_flag": None, "spec": None,
     "arms": ["neuron"], "direct_call": True},  # entity_resolver (neuron-only)
    {"id": "6", "title": "peer representation", "dir": "peerrep",
     "runner": "peerrep/peerrep_run.py",
     "build": ("peerrep/peerrep_build.py", "derive_cases.json"),
     "spec_flag": "--cases", "spec": "derive_cases.json",
     "arms": ["rag", "base", "neuron"], "direct_call": True},  # extract_user (base-tier, neuron-upgraded)
]

def _log(m): print(f"\033[36m[run_all]\033[0m {m}", file=sys.stderr)
def _warn(m): print(f"\033[33m[run_all]\033[0m {m}", file=sys.stderr)
def _err(m): print(f"\033[31m[run_all]\033[0m {m}", file=sys.stderr)

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

def _spec_path(ph):
    return HERE / ph["dir"] / "specs" / ph["spec"] if ph["spec"] else None

def _ensure_spec(ph, timeout):
    if not ph["build"]:
        return None
    specdir = HERE / ph["dir"] / "specs"; specdir.mkdir(parents=True, exist_ok=True)
    sp = specdir / ph["spec"]
    if sp.exists():
        return sp
    script, _ = ph["build"]
    r = _sh([sys.executable, HERE / script, "build", "--out", str(specdir)], timeout)
    if r.returncode != 0:
        raise RuntimeError(f"spec build failed for phase {ph['id']}: {r.stderr[-300:]}")
    if not sp.exists():
        cand = list(specdir.glob("*.json"))
        if not cand:
            raise RuntimeError(f"phase {ph['id']} produced no spec")
        return cand[0]
    return sp

def _common_args(args, arm):
    c = []
    if arm:
        c += ["--arm", arm]
    c += ["--source", args.source]
    if args.answer_model:
        c += ["--answer-model", args.answer_model]
    if args.judge_model:
        c += ["--judge-model", args.judge_model]
    if args.overlay_cmd:
        c += ["--overlay-cmd", args.overlay_cmd]
    return c

def _available_arms(args):
    """Which arms this install can actually run.
      rag   : always (pure embedding, no model, no neuron).
      base  : always present (this IS the base repo).
      neuron: ONLY if an --overlay-cmd was given (neuron is an install-time overlay;
              a base-only user has no neuron tools to drive). Auto-dropped otherwise
              so base-only installs degrade gracefully instead of failing loud."""
    avail = {"rag", "base"}
    if args.overlay_cmd:
        avail.add("neuron")
    return avail

def _selected(args):
    want_ph = set(p.strip() for p in args.phases.split(",")) if args.phases else None
    want_arm = set(a.strip() for a in args.arms.split(",")) if args.arms else None
    avail = _available_arms(args)
    out = []
    skipped_neuron_only = []
    for ph in PHASES:
        if want_ph and ph["id"] not in want_ph:
            continue
        arms = [a for a in ph["arms"]
                if (not want_arm or a in want_arm) and a in avail]
        if not arms:
            # a neuron-only phase (4b/5b) on a base-only install lands here — record
            # it so we can tell the user WHY it was skipped, not silently drop it.
            if all(a == "neuron" for a in ph["arms"]) and "neuron" not in avail:
                skipped_neuron_only.append(ph["id"])
            continue
        out.append((ph, arms))
    return out, skipped_neuron_only

def _plan(args):
    rows = []
    total_runs = 0
    sel, _skipped = _selected(args)
    for ph, arms in sel:
        if ph.get("is_ablation"):
            runs = 12  # 6 mechanisms x (baseline+ablated), baseline-cached in practice
            rows.append({"phase": ph["id"], "title": ph["title"], "arms": ["neuron"],
                         "runs": runs, "note": "6 mechanisms x2 (baseline cached per phase)"})
        else:
            runs = len(arms)
            rows.append({"phase": ph["id"], "title": ph["title"], "arms": arms, "runs": runs})
        total_runs += runs
    return rows, total_runs

def run(args):
    sel, skipped_neuron_only = _selected(args)
    if not sel:
        _err("nothing selected — check --phases/--arms filters")
        return {"ok": False, "reason": "empty_selection"}

    # Base-only install (no --overlay-cmd) => neuron arm auto-dropped, neuron-only
    # phases skipped with a clear reason. This is the graceful-degradation path.
    if skipped_neuron_only:
        _warn(f"base-only install (no --overlay-cmd): skipping neuron-only phase(s) "
              f"{','.join(skipped_neuron_only)} (4b promotion / 5b ablation need the "
              f"neuron overlay). Pass --overlay-cmd to include them.")

    if args.dry_run:
        rows, total = _plan(args)
        paid = any(a in ("base", "neuron") for _, arms in sel for a in arms)
        print(json.dumps({"ok": True, "dry_run": True, "phases": rows,
                          "total_runs": total,
                          "neuron_available": "neuron" in _available_arms(args),
                          "skipped_neuron_only": skipped_neuron_only,
                          "spend": ("PAID (base/neuron arms present)" if paid
                                    else "FREE (rag-only)"),
                          "hint": ("base-only: pass --overlay-cmd to add the neuron arm + "
                                   "phases 4b/5b" if not args.overlay_cmd
                                   else "drop base/neuron for a $0 run, or --estimate-only "
                                        "for a Phase-1 cost estimate")}, indent=2))
        return {"ok": True, "dry_run": True}

    if args.estimate_only:
        # Only Phase 1 runner supports a cost estimate; call it per arm.
        ph = next((p for p in PHASES if p["id"] == "1"), None)
        est = []
        total_tokens = 0
        for arm in [a for a in ph["arms"] if (not args.arms or a in args.arms.split(","))]:
            cmd = [sys.executable, HERE / ph["runner"], *_common_args(args, arm),
                   *ph.get("extra", []), "--estimate-only"]
            r = _sh(cmd, args.timeout)
            tail = _json_tail(r.stdout) or r.stdout[-400:]
            # TOKEN-first: sum est_total_tokens across arms (the real cost on subs).
            # tail is a JSON-string fragment (run.py emits the estimate pre-serialized),
            # so scrape the field directly rather than json.loads the partial object.
            m = re.search(r'"est_total_tokens":\s*(\d+)', tail or "")
            if m:
                total_tokens += int(m.group(1))
            est.append({"arm": arm, "estimate": tail})
        # Phase-1 only; note this is NOT the whole program's token cost.
        print(json.dumps({"ok": True, "estimate_only": True, "phase1": est,
                          "phase1_total_tokens_all_arms": total_tokens,
                          "scope_note": "TOKENS are the headline cost on subscription plans. "
                                        "This sum covers PHASE 1 (LongMemEval) ONLY across the "
                                        "selected arms; the full 13-phase program runs many more."},
                         indent=2))
        return {"ok": True, "estimate_only": True}

    if not args.answer_model and any(a in ("base", "neuron") for _, arms in sel for a in arms):
        _warn("base/neuron selected without --answer-model — those arms will FAIL "
              "(no LLM to build memory). Continuing; rag arm still runs. Ctrl-C to abort.")

    t0 = time.time()
    results = {}
    completed, failed = [], []
    for ph, arms in sel:
        try:
            spec = _ensure_spec(ph, args.timeout)
        except RuntimeError as e:
            _err(str(e)); failed.append({"phase": ph["id"], "reason": str(e)}); continue

        if ph.get("is_ablation"):
            _log(f"phase {ph['id']} {ph['title']} — ablation orchestrator")
            cmd = [sys.executable, HERE / ph["runner"],
                   "--source", args.source]
            if args.answer_model: cmd += ["--answer-model", args.answer_model]
            if args.judge_model: cmd += ["--judge-model", args.judge_model]
            if args.overlay_cmd: cmd += ["--overlay-cmd", args.overlay_cmd]
            r = _sh(cmd, args.timeout * 4)
            res = _json_tail(r.stdout)
            (results.setdefault(ph["id"], {}))["neuron"] = res
            (completed if res.get("ok") else failed).append(
                {"phase": ph["id"], "arm": "neuron", "reason": res.get("reason")})
            continue

        for arm in arms:
            _log(f"phase {ph['id']} {ph['title']} — arm {arm}")
            cmd = [sys.executable, HERE / ph["runner"], *_common_args(args, arm),
                   *ph.get("extra", [])]
            if spec is not None and ph["spec_flag"]:
                cmd += [ph["spec_flag"], str(spec)]
            r = _sh(cmd, args.timeout)
            res = _json_tail(r.stdout)
            (results.setdefault(ph["id"], {}))[arm] = res
            ok = res.get("ok")
            (completed if ok else failed).append(
                {"phase": ph["id"], "arm": arm, "reason": res.get("reason")})
            if not ok:
                _warn(f"  phase {ph['id']}/{arm} did not complete: {res.get('reason')} "
                      f":: {r.stderr[-200:]}")

    # Always render the scorecard (aggregates whatever completed).
    _log("rendering unified scorecard")
    sc_cmd = [sys.executable, HERE / "scorecard" / "scorecard.py", "--json"]
    sc = _sh(sc_cmd, 300)
    sc_out = _json_tail(sc.stdout)

    summary = {"ok": len(failed) == 0, "seconds": round(time.time() - t0, 1),
               "completed": completed, "failed": failed,
               "scorecard": sc_out.get("out") if sc_out else None,
               "note": ("all phases completed" if not failed
                        else f"{len(failed)} phase/arm run(s) failed — scorecard shows the rest")}
    (HERE / "results").mkdir(exist_ok=True)
    (HERE / "results" / "run_all_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return summary

def main():
    ap = argparse.ArgumentParser(
        description="ONE command to run the whole dinomem eval program (all phases, in order).",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--source", default=os.environ.get("DINOMEM_WORKSPACE", ""),
                    help="the workspace to evaluate (or set DINOMEM_WORKSPACE)")
    ap.add_argument("--answer-model", default=os.environ.get("DINOMEM_BENCH_ANSWER_MODEL", ""),
                    help="LLM that answers probes (required for base/neuron arms)")
    ap.add_argument("--judge-model", default=os.environ.get("DINOMEM_BENCH_JUDGE_MODEL", ""),
                    help="LLM judge for open-ended scoring")
    ap.add_argument("--overlay-cmd", default=os.environ.get("DINOMEM_BENCH_OVERLAY_CMD", ""),
                    help="neuron install command with {ws} placeholder (required for neuron arm)")
    ap.add_argument("--arms", default="", help="comma list subset of rag,base,neuron (default: all)")
    ap.add_argument("--phases", default="", help="comma list subset of phase ids e.g. 1,3a,5b (default: all)")
    ap.add_argument("--dry-run", action="store_true", help="print the full plan + run count, no spend")
    ap.add_argument("--estimate-only", action="store_true", help="Phase-1 cost estimate per arm, no run")
    ap.add_argument("--timeout", type=int, default=1200, help="per-run timeout seconds")
    args = ap.parse_args()
    if not args.source and not (args.dry_run):
        _err("--source (or DINOMEM_WORKSPACE) required")
        sys.exit(2)
    res = run(args)
    sys.exit(0 if res.get("ok") else 1)

if __name__ == "__main__":
    main()
