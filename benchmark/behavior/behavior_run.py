#!/usr/bin/env python3
"""
behavior_run.py — Phase 4 (4c): behavior-change A/B loop.

Drives ONE arm, then answers each held-out probe under TWO recall conditions and
measures whether MEMORY changed the response:

  MEMORY-ON  = the arm's real recall (rag query / base lexical / neuron hybrid).
  MEMORY-OFF = the SAME model + prompt but recall returns NOTHING (empty context).

Each answer is checked DETERMINISTICALLY against the scenario rubric (forbidden /
required keywords) — constraint satisfied or not. Then:

  behavior_change_rate = fraction of probes where ON satisfies AND OFF does not
                         (memory demonstrably changed the response for the better).
  on_constraint_pct    = fraction of ON answers satisfying the constraint.
  off_constraint_pct   = fraction of OFF answers satisfying it (guessable-without-
                         memory baseline). The ON-OFF DELTA is the causal effect.

WHY A/B ON RECALL: same model, same prompt, same probe — only the remembered
context differs. So any delta is attributable to MEMORY, not the answerer. This is
the on/off ablation the strategy PDF asks for.

The rubric check is judge-free (keyword satisfy), so 4c's headline runs WITHOUT a
paid judge — BUT composing the answers still needs a real answer model (offline
--stub-judge produces a canned stub answer that ignores context, so ON==OFF and
behavior_change_rate=0 by construction — only a smoke/plumbing check, not a real
measurement). Real measurement = --answer-model <real>.

Usage:
  python3 behavior_run.py --arm neuron --scenarios <s.json> --source <ws> \\
      --answer-model M --overlay-cmd '<neuron install at {ws}>' \\
      [--out results/behavior_<arm>.json]
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
LM = HERE.parent / "longmemeval"
RESULTS = HERE / "results"
BUILD = HERE / "behavior_build.py"

def _log(m): print(f"[behavior_run] {m}", file=sys.stderr)
def _fail(m):
    print(json.dumps({"ok": False, "reason": m}))
    raise SystemExit(f"[behavior_run] FAIL: {m}")

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
        j = s.find("{")
        try:
            return json.loads(s[j:])
        except Exception:  # noqa: BLE001
            return {}

def _labroot(lab_info):
    sd = lab_info.get("sessions_dir")
    return sd.rsplit("/sessions", 1)[0] if sd else lab_info["lab"]

def _drive_arm(arm, lab_info, source, overlay_cmd, timeout):
    lab = lab_info["lab"]; ws = lab_info.get("workspace", lab)
    mtime = lab_info["live_source_mtime"]
    if arm == "rag":
        ir = _sh([sys.executable, LM / "rag_recall.py", "index", "--lab", lab], timeout)
        if ir.returncode != 0:
            _fail(f"rag index failed: {ir.stderr[-300:]}")
        return {"ok": True, "arm": "rag",
                "recall_cmd": (f'{sys.executable} {LM / "rag_recall.py"} query '
                               f'--lab {lab} "{{q}}" --k {{k}} --json')}
    if arm == "neuron":
        if not overlay_cmd:
            _fail("--arm neuron requires --overlay-cmd")
        ov = _sh(["bash", "-c", overlay_cmd.replace("{ws}", ws).replace("{lab}", lab)], timeout)
        if ov.returncode != 0:
            _fail(f"neuron overlay failed: {ov.stderr[-300:]}")
        _dn = [sys.executable, LM / "drive_neuron.py", "--ws", ws,
               "--sandbox-root", lab, "--live-source", source,
               "--live-source-mtime", mtime, "--json"]
        for _sk in filter(None, os.environ.get("DINOMEM_BENCH_SKIP_STAGE", "").split(",")):
            _dn += ["--skip-stage", _sk]   # Phase 5b ablation forward
        dr = _sh(_dn, timeout)
        res = _json_tail(dr.stdout)
        if dr.returncode != 0 or not res.get("ok"):
            _fail(f"neuron drive did not converge: {res.get('reason')} :: {dr.stderr[-300:]}")
        res["recall_cmd"] = (f'{sys.executable} {ws}/tools/hybrid_recall.py "{{q}}" '
                             f'--k {{k}} --json')
        return res
    dr = _sh([sys.executable, LM / "drive_base.py", "--lab", lab, "--live-source", source,
              "--live-source-mtime", mtime, "--json"], timeout)
    res = _json_tail(dr.stdout)
    if dr.returncode != 0 or not res.get("ok"):
        _fail(f"base drive did not converge: {res.get('reason')} :: {dr.stderr[-300:]}")
    return res

def _answer(lab, questions_file, out_hyp, answer_model, recall, stub_judge,
            recall_cmd, memory_on, timeout):
    """Run answer.py once. memory_on=False forces empty context via the
    'none' recall mode (answer.py must support --recall none = no retrieval).
    We pass DINOMEM_BENCH_RECALL_CMD only when memory_on."""
    ans_cmd = [sys.executable, LM / "answer.py", "--lab", lab, "--dataset", questions_file,
               "--out", out_hyp, "--json"]
    ans_cmd += ["--recall", recall if memory_on else "none"]
    if stub_judge:
        ans_cmd += ["--model", "stub"]
    elif answer_model:
        ans_cmd += ["--model", answer_model]
    env = dict(os.environ)
    if memory_on and recall_cmd:
        env["DINOMEM_BENCH_RECALL_CMD"] = recall_cmd
    ar = _sh(ans_cmd, timeout, env=env)
    if ar.returncode != 0:
        _fail(f"answer.py ({'on' if memory_on else 'off'}) failed: {ar.stderr[-300:]}")
    hyps = {}
    for line in Path(out_hyp).read_text().splitlines():
        if not line.strip():
            continue
        o = json.loads(line)
        hyps[o.get("question_id")] = o.get("hypothesis", "")
    return hyps

def _satisfies(answer: str, rubric: dict) -> bool:
    """Deterministic constraint check. forbidden: none may appear. required: per
    required_mode ('any' => at least one; default 'all' => all must appear)."""
    a = (answer or "").lower()
    for f in rubric.get("forbidden", []):
        if f.lower() in a:
            return False
    req = rubric.get("required", [])
    if req:
        mode = rubric.get("required_mode", "all")
        hits = [r for r in req if r.lower() in a]
        if mode == "any":
            if not hits:
                return False
        else:
            if len(hits) != len(req):
                return False
    return True

def run_arm(args):
    RESULTS.mkdir(parents=True, exist_ok=True)
    spec = json.loads(Path(args.scenarios).read_text())
    recall = args.recall or ("base" if args.arm == "base" else "command")
    layout = "real" if args.arm == "neuron" else "flat"
    t0 = time.time()
    setup = _sh([sys.executable, LM / "setup_lab.py", "--source", args.source,
                 "--layout", layout, "--json"], args.timeout)
    if setup.returncode != 0:
        _fail(f"setup_lab failed: {setup.stderr[-300:]}")
    lab_info = _json_tail(setup.stdout)
    labroot = _labroot(lab_info); lab = lab_info["lab"]
    try:
        em = _sh([sys.executable, BUILD, "emit", "--scenarios", args.scenarios, "--lab", labroot, "--json"], args.timeout)
        if em.returncode != 0:
            _fail(f"emit failed: {em.stderr[-300:]}")
        qf = RESULTS / f"{args.arm}_behavior_questions.json"
        gd = _sh([sys.executable, BUILD, "gold", "--scenarios", args.scenarios, "--lab", labroot, "--out", qf], args.timeout)
        if gd.returncode != 0:
            _fail(f"gold failed: {gd.stderr[-300:]}")
        questions = json.loads(qf.read_text())
        rubrics = {q["question_id"]: q["rubric"] for q in questions}

        drive_res = _drive_arm(args.arm, lab_info, args.source, args.overlay_cmd, args.timeout)
        rc = drive_res.get("recall_cmd", "")
        on = _answer(lab, str(qf), str(RESULTS / f"{args.arm}_behavior_on.jsonl"),
                     args.answer_model, recall, args.stub_judge, rc, True, args.timeout)
        off = _answer(lab, str(qf), str(RESULTS / f"{args.arm}_behavior_off.jsonl"),
                      args.answer_model, recall, args.stub_judge, rc, False, args.timeout)
    finally:
        if not args.keep_lab:
            _sh([sys.executable, LM / "setup_lab.py", "--teardown", lab], 120)

    per = []
    changed = on_ok = off_ok = 0
    for qid, rub in rubrics.items():
        a_on = _satisfies(on.get(qid, ""), rub)
        a_off = _satisfies(off.get(qid, ""), rub)
        on_ok += a_on; off_ok += a_off
        chg = a_on and not a_off
        changed += chg
        per.append({"question_id": qid, "on_satisfies": a_on, "off_satisfies": a_off,
                    "behavior_changed": chg})
    n = len(rubrics)
    result = {"ok": True, "arm": args.arm, "schema": spec["schema"], "seed": spec["seed"],
              "answer_model": args.answer_model or ("stub" if args.stub_judge else "gateway-default"),
              "stub_judge": bool(args.stub_judge), "n_scenarios": n,
              "behavior_change_rate": round(changed / n, 3) if n else None,
              "on_constraint_pct": round(100 * on_ok / n, 1) if n else None,
              "off_constraint_pct": round(100 * off_ok / n, 1) if n else None,
              "causal_delta_pct": round(100 * (on_ok - off_ok) / n, 1) if n else None,
              "per_probe": per,
              "note": ("A/B on recall condition (same model/prompt/probe). "
                       "stub answer ignores context => on==off => rate 0 (plumbing only); "
                       "use --answer-model <real> for a real measurement."),
              "seconds": round(time.time() - t0, 1)}
    _log(f"  behavior_change_rate={result['behavior_change_rate']} "
         f"on={result['on_constraint_pct']}% off={result['off_constraint_pct']}% "
         f"delta={result['causal_delta_pct']}%")
    out = Path(args.out) if args.out else RESULTS / f"behavior_{args.arm}.json"
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    result["out"] = str(out)
    return result

def main():
    ap = argparse.ArgumentParser(description="Phase 4c behavior-change A/B loop (one arm)")
    ap.add_argument("--arm", choices=["rag", "base", "neuron"], default="neuron")
    ap.add_argument("--scenarios", required=True)
    ap.add_argument("--source", default=os.environ.get("DINOMEM_WORKSPACE", ""))
    ap.add_argument("--answer-model", default=os.environ.get("DINOMEM_BENCH_ANSWER_MODEL", ""))
    ap.add_argument("--recall", choices=["base", "command"], default=None)
    ap.add_argument("--overlay-cmd", default=os.environ.get("DINOMEM_BENCH_OVERLAY_CMD", ""))
    ap.add_argument("--stub-judge", action="store_true", help="plumbing smoke only (on==off)")
    ap.add_argument("--out", default="")
    ap.add_argument("--keep-lab", action="store_true")
    ap.add_argument("--timeout", type=int, default=900)
    args = ap.parse_args()
    if not args.source:
        _fail("--source (or DINOMEM_WORKSPACE) required")
    print(json.dumps(run_arm(args), indent=2))

if __name__ == "__main__":
    main()
