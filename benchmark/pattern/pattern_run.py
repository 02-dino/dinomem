#!/usr/bin/env python3
"""
pattern_run.py — Phase 4 (4a): latent-pattern / relationship drive + probe loop.

Drives ONE arm (rag|base|neuron) over the relationship-chain test set and reports:

  direct_acc      — accuracy on single-stated-edge probes (control; every arm
                    should pass — separates "can't retrieve" from "can't compose").
  multihop_acc    — accuracy on probes whose answer requires COMPOSING >=2 stated
                    edges (never stated verbatim). THE headline: pattern discovery.
  compose_delta   — multihop_acc - (rag multihop_acc). For neuron this is the
                    graph-leg contribution over the flat-RAG floor.

WHY: a flat RAG store holds each edge as a separate chunk with no traversal; it can
answer direct edges but structurally cannot compose a 2-3 hop chain. Neuron's L2
entity graph (memory_graph.py depends_on/affects/trace + hybrid_recall graph leg)
is the mechanism under test. For the neuron arm the driver also asserts graph
nodes>0 (drive_neuron already builds L2) so a zero-graph regression fails loud.

PROTOCOL mirrors supersession_run: fresh isolated lab -> emit chain haystack ->
direct+multihop gold -> drive to convergence -> answer+score -> split by kind ->
teardown. Reuses Phase-1 setup_lab/answer/score/drive_* UNCHANGED. Offline-verifiable
with --stub-judge (rag arm only; base/neuron need a real extract model).

Usage:
  python3 pattern_run.py --arm rag --chains <c.json> --source <ws> \\
      [--answer-model M] [--judge-model M] [--stub-judge] \\
      [--overlay-cmd '<neuron install at {ws}>'] [--out results/pattern_<arm>.json]
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
LM = HERE.parent / "longmemeval"
RESULTS = HERE / "results"
BUILD = HERE / "pattern_build.py"

def _log(m): print(f"[pattern_run] {m}", file=sys.stderr)
def _fail(m):
    print(json.dumps({"ok": False, "reason": m}))
    raise SystemExit(f"[pattern_run] FAIL: {m}")

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
        recall_cmd = (f'{sys.executable} {LM / "rag_recall.py"} query '
                      f'--lab {lab} "{{q}}" --k {{k}} --json')
        return {"ok": True, "arm": "rag", "recall_cmd": recall_cmd}
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
        # 4a-specific: the graph leg is the compose mechanism — assert it exists.
        if res.get("graph_nodes", 1) == 0:
            _fail("neuron graph has 0 nodes — compose mechanism absent (regression)")
        res["recall_cmd"] = (f'{sys.executable} {ws}/tools/hybrid_recall.py "{{q}}" '
                             f'--k {{k}} --json')
        return res
    dr = _sh([sys.executable, LM / "drive_base.py", "--lab", lab, "--live-source", source,
              "--live-source-mtime", mtime, "--json"], timeout)
    res = _json_tail(dr.stdout)
    if dr.returncode != 0 or not res.get("ok"):
        _fail(f"base drive did not converge: {res.get('reason')} :: {dr.stderr[-300:]}")
    return res

def _probe(lab_info, questions_file, answer_model, judge, recall, stub_judge,
           out_prefix, timeout, recall_cmd=""):
    lab = Path(lab_info["lab"])
    hyp = RESULTS / f"{out_prefix}_hyp.jsonl"
    metrics_out = RESULTS / f"{out_prefix}_metrics.json"
    ans_cmd = [sys.executable, LM / "answer.py", "--lab", lab, "--dataset", questions_file,
               "--out", hyp, "--recall", recall, "--json"]
    if stub_judge:
        ans_cmd += ["--model", "stub"]
    elif answer_model:
        ans_cmd += ["--model", answer_model]
    ans_env = dict(os.environ)
    if recall_cmd:
        ans_env["DINOMEM_BENCH_RECALL_CMD"] = recall_cmd
    ar = _sh(ans_cmd, timeout, env=ans_env)
    if ar.returncode != 0:
        _fail(f"answer.py failed: {ar.stderr[-300:]}")
    score_cmd = [sys.executable, LM / "score.py", "--hyp", hyp, "--ref", questions_file,
                 "--lab", str(lab), "--metrics-out", metrics_out, "--json"]
    score_cmd += ["--judge", "stub"] if stub_judge else (["--judge", judge] if judge else [])
    sr = _sh(score_cmd, timeout)
    if sr.returncode != 0:
        _fail(f"score.py failed: {sr.stderr[-300:]}")
    try:
        return json.loads(Path(metrics_out).read_text()), hyp
    except Exception as e:  # noqa: BLE001
        _fail(f"cannot read metrics {metrics_out}: {e}")

def _split_by_kind(hyp_path, ref):
    cand = list(hyp_path.parent.glob(hyp_path.name + ".eval-results-*"))
    if not cand:
        return {"note": "no eval-results file"}
    direct, multi = [], []
    for line in cand[0].read_text().splitlines():
        if not line.strip():
            continue
        e = json.loads(line)
        lab = 1 if (e.get("autoeval_label") or {}).get("label") else 0
        qt = (ref.get(e.get("question_id")) or {}).get("question_type", "")
        if qt == "pattern-direct":
            direct.append(lab)
        elif qt == "pattern-multihop":
            multi.append(lab)
    def _pct(v):
        return round(100 * sum(v) / len(v), 1) if v else None
    return {"direct_acc": _pct(direct), "direct_n": len(direct),
            "multihop_acc": _pct(multi), "multihop_n": len(multi)}

def run_arm(args):
    RESULTS.mkdir(parents=True, exist_ok=True)
    spec = json.loads(Path(args.chains).read_text())
    recall = args.recall or ("base" if args.arm == "base" else "command")
    layout = "real" if args.arm == "neuron" else "flat"
    t0 = time.time()
    setup = _sh([sys.executable, LM / "setup_lab.py", "--source", args.source,
                 "--layout", layout, "--json"], args.timeout)
    if setup.returncode != 0:
        _fail(f"setup_lab failed: {setup.stderr[-300:]}")
    lab_info = _json_tail(setup.stdout)
    labroot = _labroot(lab_info)
    try:
        em = _sh([sys.executable, BUILD, "emit", "--chains", args.chains, "--lab", labroot, "--json"], args.timeout)
        if em.returncode != 0:
            _fail(f"emit failed: {em.stderr[-300:]}")
        qf = RESULTS / f"{args.arm}_pattern_questions.json"
        gd = _sh([sys.executable, BUILD, "gold", "--chains", args.chains, "--lab", labroot, "--out", qf], args.timeout)
        if gd.returncode != 0:
            _fail(f"gold failed: {gd.stderr[-300:]}")
        drive_res = _drive_arm(args.arm, lab_info, args.source, args.overlay_cmd, args.timeout)
        judge = "stub" if args.stub_judge else args.judge_model
        m, hyp = _probe(lab_info, str(qf), args.answer_model, judge, recall, args.stub_judge,
                        f"{args.arm}_pattern", args.timeout, recall_cmd=drive_res.get("recall_cmd", ""))
        ref = {e["question_id"]: e for e in json.loads(qf.read_text())}
        split = _split_by_kind(hyp, ref)
        _log(f"  direct_acc={split.get('direct_acc')}% multihop_acc={split.get('multihop_acc')}%")
    finally:
        if not args.keep_lab:
            _sh([sys.executable, LM / "setup_lab.py", "--teardown", lab_info["lab"]], 120)

    result = {"ok": True, "arm": args.arm, "schema": spec["schema"], "seed": spec["seed"],
              "answer_model": args.answer_model or ("stub" if args.stub_judge else "gateway-default"),
              "judge_model": "stub" if args.stub_judge else (args.judge_model or "gateway-default"),
              "stub_judge": bool(args.stub_judge),
              "n_chains": spec["n_chains"], "n_edges": spec["n_edges"],
              "overall_accuracy": m.get("overall_accuracy"),
              "direct_acc": split.get("direct_acc"), "direct_n": split.get("direct_n"),
              "multihop_acc": split.get("multihop_acc"), "multihop_n": split.get("multihop_n"),
              "graph_nodes": drive_res.get("graph_nodes"),
              "seconds": round(time.time() - t0, 1)}
    out = Path(args.out) if args.out else RESULTS / f"pattern_{args.arm}.json"
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    result["out"] = str(out)
    return result

def main():
    ap = argparse.ArgumentParser(description="Phase 4a latent-pattern loop (one arm)")
    ap.add_argument("--arm", choices=["rag", "base", "neuron"], default="rag")
    ap.add_argument("--chains", required=True)
    ap.add_argument("--source", default=os.environ.get("DINOMEM_WORKSPACE", ""))
    ap.add_argument("--answer-model", default=os.environ.get("DINOMEM_BENCH_ANSWER_MODEL", ""))
    ap.add_argument("--judge-model", default=os.environ.get("DINOMEM_BENCH_JUDGE_MODEL", ""))
    ap.add_argument("--recall", choices=["base", "command"], default=None)
    ap.add_argument("--overlay-cmd", default=os.environ.get("DINOMEM_BENCH_OVERLAY_CMD", ""))
    ap.add_argument("--stub-judge", action="store_true")
    ap.add_argument("--out", default="")
    ap.add_argument("--keep-lab", action="store_true")
    ap.add_argument("--timeout", type=int, default=900)
    args = ap.parse_args()
    if not args.source:
        _fail("--source (or DINOMEM_WORKSPACE) required")
    print(json.dumps(run_arm(args), indent=2))

if __name__ == "__main__":
    main()
