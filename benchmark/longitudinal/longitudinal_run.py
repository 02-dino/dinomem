#!/usr/bin/env python3
"""
longitudinal_run.py — Phase 2 (2a): the incremental drive + probe loop.

Drives ONE arm (rag|base|neuron) across the longitudinal timeline's CHECKPOINTS,
re-probing the SAME evolving-fact questions at each checkpoint with as-of gold, and
emits an ACCURACY-VS-SESSIONS curve. This is the Phase-2 measurement Phase-1 can't
make: does the memory corpus track the LATEST truth as sessions accumulate, or does
stale/superseded content pollute recall and drag accuracy down over time?

PROTOCOL (per checkpoint K in the timeline):
  1. build a fresh isolated lab (reuse Phase-1 setup_lab.py — same isolation +
     .dinomem_lab sentinel + SESSIONS_DIR patch discipline).
  2. emit the timeline haystack THROUGH day K (only sessions that existed by then).
  3. write the as-of-K probe gold + question file.
  4. drive the arm pipeline to convergence (rag=index only; base/neuron=real front
     door), with the SAME isolation tripwire as Phase 1.
  5. answer + score the probes (reuse Phase-1 answer.py + score.py UNCHANGED).
  6. record overall accuracy + per-kind (stable vs superseding) + per-fact hit.
  7. teardown the lab.
Then assemble the per-checkpoint curve + a stable-vs-superseding split (the split is
the real story: stable should stay ~flat-high; superseding is where a weak memory
system decays).

Everything reuses the Phase-1 harness modules by path so there is ONE shared
protocol. Models stay selectable (same env/flags as Phase 1). No spend unless a
real answer/judge model is passed — a --stub-judge path exists for offline CI.

Usage:
  python3 longitudinal_run.py --arm rag --timeline <t.json> --source <ws> \\
      [--answer-model M] [--judge-model M] [--checkpoints 1,8,20,30] \\
      [--stub-judge] [--out results/longitudinal_<arm>.json] [--keep-lab]
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
LM = HERE.parent / "longmemeval"          # Phase-1 harness modules live here
RESULTS = HERE / "results"
BUILD = HERE / "longitudinal_build.py"


def _log(m: str) -> None:
    print(f"[longitudinal_run] {m}", file=sys.stderr)


def _fail(m: str) -> None:
    print(json.dumps({"ok": False, "reason": m}))
    raise SystemExit(f"[longitudinal_run] FAIL: {m}")


def _sh(cmd, timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run([str(c) for c in cmd], capture_output=True,
                          text=True, timeout=timeout)


def _json_tail(s: str) -> dict:
    """Parse the last JSON object printed on stdout (tools print a JSON summary)."""
    i = s.rfind("{")
    if i < 0:
        return {}
    try:
        return json.loads(s[i:])
    except Exception:  # noqa: BLE001
        # some tools print multiple objects; fall back to first
        j = s.find("{")
        try:
            return json.loads(s[j:])
        except Exception:  # noqa: BLE001
            return {}


def _drive_arm(arm: str, lab_info: dict, source: str, overlay_cmd: str,
               timeout: int) -> dict:
    """Drive the arm's pipeline to convergence in the lab (mirrors run.py's arm
    dispatch). rag = no pipeline (index built at answer time by rag_recall);
    base = drive_base.py; neuron = overlay then drive_neuron.py."""
    lab = lab_info["lab"]
    ws = lab_info.get("workspace", lab)
    mtime = lab_info["live_source_mtime"]
    if arm == "rag":
        # naive floor: retrieval over the raw haystack, no distilled memory to
        # converge. Build the index now so answer.py's recall hook can query it.
        ir = _sh([sys.executable, LM / "rag_recall.py", "index", "--lab", lab], timeout)
        if ir.returncode != 0:
            _fail(f"rag index failed: {ir.stderr[-300:]}")
        # answer.py's --recall command shells DINOMEM_BENCH_RECALL_CMD ({q}/{k}).
        # Auto-wire it to this lab's rag_recall query (mirrors run.py line 403-407).
        recall_cmd = (f'{sys.executable} {LM / "rag_recall.py"} query '
                      f'--lab {lab} "{{q}}" --k {{k}} --json')
        return {"ok": True, "arm": "rag", "note": "no-pipeline", "recall_cmd": recall_cmd}
    if arm == "neuron":
        if not overlay_cmd:
            _fail("--arm neuron requires --overlay-cmd (neuron overlay onto the lab WS)")
        ov = _sh(["bash", "-c", overlay_cmd.replace("{ws}", ws).replace("{lab}", lab)], timeout)
        if ov.returncode != 0:
            _fail(f"neuron overlay failed: {ov.stderr[-300:]}")
        dr = _sh([sys.executable, LM / "drive_neuron.py", "--ws", ws,
                  "--sandbox-root", lab, "--live-source", source,
                  "--live-source-mtime", mtime, "--json"], timeout)
    else:
        dr = _sh([sys.executable, LM / "drive_base.py", "--lab", lab,
                  "--live-source", source, "--live-source-mtime", mtime, "--json"], timeout)
    res = _json_tail(dr.stdout)
    if dr.returncode != 0 or not res.get("ok"):
        _fail(f"{arm} drive did not converge: {res.get('reason')} :: {dr.stderr[-300:]}")
    return res


def _probe_checkpoint(arm, lab_info, questions_file, answer_model, judge_model,
                      recall, stub_judge, out_prefix, timeout, recall_cmd="") -> dict:
    """answer + score the as-of probes at one checkpoint. Reuses Phase-1
    answer.py + score.py UNCHANGED. Returns the score metrics dict."""
    lab = Path(lab_info["lab"])
    hyp = RESULTS / f"{out_prefix}_hyp.jsonl"
    metrics_out = RESULTS / f"{out_prefix}_metrics.json"
    ans_cmd = [sys.executable, LM / "answer.py", "--lab", lab,
               "--dataset", questions_file, "--out", hyp, "--recall", recall, "--json"]
    if answer_model:
        ans_cmd += ["--model", answer_model]
    ans_env = dict(os.environ)
    if recall_cmd:
        ans_env["DINOMEM_BENCH_RECALL_CMD"] = recall_cmd
    ar = subprocess.run([str(c) for c in ans_cmd], capture_output=True,
                        text=True, timeout=timeout, env=ans_env)
    if ar.returncode != 0:
        _fail(f"answer.py failed: {ar.stderr[-300:]}")
    score_cmd = [sys.executable, LM / "score.py", "--hyp", hyp, "--ref", questions_file,
                 "--lab", str(lab), "--metrics-out", metrics_out, "--json"]
    if judge_model:
        score_cmd += ["--judge", judge_model]
    env = dict(os.environ)
    if stub_judge:
        env["DINOMEM_BENCH_JUDGE_STUB"] = "1"   # score.py honors this for offline CI
    sr = subprocess.run([str(c) for c in score_cmd], capture_output=True,
                        text=True, timeout=timeout, env=env)
    if sr.returncode != 0:
        _fail(f"score.py failed: {sr.stderr[-300:]}")
    try:
        return json.loads(Path(metrics_out).read_text())
    except Exception as e:  # noqa: BLE001
        _fail(f"cannot read metrics {metrics_out}: {e}")


def _per_kind_split(hyp_path: Path, ref: dict) -> dict:
    """From the graded eval-results, split accuracy stable vs superseding using the
    question_type ('longitudinal-stable' | 'longitudinal-superseding'). This is the
    real story: stable should stay flat-high; superseding is where a weak memory
    system decays as supersessions accumulate."""
    results = hyp_path.parent / (hyp_path.name.replace("_hyp.jsonl", "_hyp.jsonl"))
    # score.py writes eval-results next to the hyp file; find it
    cand = list(hyp_path.parent.glob(hyp_path.name + ".eval-results-*"))
    split = {"stable": [], "superseding": []}
    if not cand:
        return {"stable": None, "superseding": None, "note": "no eval-results file"}
    for line in cand[0].read_text().splitlines():
        if not line.strip():
            continue
        e = json.loads(line)
        qid = e.get("question_id")
        lab = 1 if (e.get("autoeval_label") or {}).get("label") else 0
        qt = (ref.get(qid) or {}).get("question_type", "")
        if qt.endswith("superseding"):
            split["superseding"].append(lab)
        elif qt.endswith("stable"):
            split["stable"].append(lab)
    def _acc(v):
        return round(sum(v) / len(v), 4) if v else None
    return {"stable": _acc(split["stable"]), "stable_n": len(split["stable"]),
            "superseding": _acc(split["superseding"]),
            "superseding_n": len(split["superseding"])}


def run_arm(args) -> dict:
    """Drive one arm across all checkpoints -> accuracy-vs-sessions curve."""
    RESULTS.mkdir(parents=True, exist_ok=True)
    timeline = json.loads(Path(args.timeline).read_text())
    all_ckpts = [c["through_day"] for c in timeline["checkpoints"]]
    if args.checkpoints:
        want = {int(x) for x in args.checkpoints.split(",")}
        ckpts = [k for k in all_ckpts if k in want]
    else:
        ckpts = all_ckpts
    if not ckpts:
        _fail(f"no checkpoints selected (available: {all_ckpts})")
    recall = args.recall or ("base" if args.arm == "base" else "command")
    layout = "real" if args.arm == "neuron" else "flat"
    curve = []
    t0 = time.time()
    for k in ckpts:
        _log(f"=== checkpoint through_day={k} (arm={args.arm}) ===")
        # 1. fresh isolated lab
        setup = _sh([sys.executable, LM / "setup_lab.py", "--source", args.source,
                     "--layout", layout, "--json"], args.timeout)
        if setup.returncode != 0:
            _fail(f"setup_lab failed: {setup.stderr[-300:]}")
        lab_info = _json_tail(setup.stdout)
        lab = lab_info["lab"]
        try:
            # 2. emit haystack through day k
            em = _sh([sys.executable, BUILD, "emit", "--timeline", args.timeline,
                      "--lab", lab_info.get("sessions_dir", lab).rsplit("/sessions", 1)[0],
                      "--through-day", k, "--json"], args.timeout)
            if em.returncode != 0:
                _fail(f"emit failed: {em.stderr[-300:]}")
            # 3. as-of gold + question file
            qf = RESULTS / f"{args.arm}_d{k}_questions.json"
            gd = _sh([sys.executable, BUILD, "gold", "--timeline", args.timeline,
                      "--lab", lab_info.get("sessions_dir", lab).rsplit("/sessions", 1)[0],
                      "--through-day", k, "--out", qf], args.timeout)
            if gd.returncode != 0:
                _fail(f"gold failed: {gd.stderr[-300:]}")
            # 4. drive the arm to convergence
            drive_res = _drive_arm(args.arm, lab_info, args.source, args.overlay_cmd,
                                   args.timeout)
            # 5. answer + score the probes
            judge = "stub" if args.stub_judge else args.judge_model
            m = _probe_checkpoint(args.arm, lab_info, str(qf), args.answer_model,
                                  judge, recall, args.stub_judge,
                                  f"{args.arm}_d{k}", args.timeout,
                                  recall_cmd=drive_res.get("recall_cmd", ""))
            ref = {e["question_id"]: e for e in json.loads(qf.read_text())}
            split = _per_kind_split(RESULTS / f"{args.arm}_d{k}_hyp.jsonl", ref)
            curve.append({
                "through_day": k,
                "n_sessions": k,               # one session per day
                "overall_accuracy": m.get("overall_accuracy"),
                "n_probes": m.get("n_graded"),
                "stable_accuracy": split.get("stable"),
                "superseding_accuracy": split.get("superseding"),
            })
            _log(f"  through_day={k}: overall={m.get('overall_accuracy')} "
                 f"stable={split.get('stable')} superseding={split.get('superseding')}")
        finally:
            if not args.keep_lab:
                _sh([sys.executable, LM / "setup_lab.py", "--teardown", lab], 120)

    result = {
        "ok": True,
        "arm": args.arm,
        "schema": timeline["schema"],
        "seed": timeline["seed"],
        "answer_model": args.answer_model or "gateway-default",
        "judge_model": "stub" if args.stub_judge else (args.judge_model or "gateway-default"),
        "stub_judge": bool(args.stub_judge),
        "checkpoints": ckpts,
        "curve": curve,
        "seconds": round(time.time() - t0, 1),
    }
    out = Path(args.out) if args.out else RESULTS / f"longitudinal_{args.arm}.json"
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    result["out"] = str(out)
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description="Phase 2 longitudinal drive+probe loop (one arm)")
    ap.add_argument("--arm", choices=["rag", "base", "neuron"], default="rag")
    ap.add_argument("--timeline", required=True, help="timeline.json from longitudinal_build")
    ap.add_argument("--source", default=os.environ.get("DINOMEM_WORKSPACE", ""),
                    help="installed dinomem workspace (source of procedures/tools)")
    ap.add_argument("--answer-model", default=os.environ.get("DINOMEM_BENCH_ANSWER_MODEL", ""))
    ap.add_argument("--judge-model", default=os.environ.get("DINOMEM_BENCH_JUDGE_MODEL", ""))
    ap.add_argument("--recall", choices=["base", "command"], default=None)
    ap.add_argument("--overlay-cmd", default=os.environ.get("DINOMEM_BENCH_OVERLAY_CMD", ""))
    ap.add_argument("--checkpoints", default="", help="comma list subset, e.g. 1,8,20,30")
    ap.add_argument("--stub-judge", action="store_true",
                    help="offline CI: deterministic substring grader, no spend, non-canonical")
    ap.add_argument("--out", default="")
    ap.add_argument("--keep-lab", action="store_true")
    ap.add_argument("--timeout", type=int, default=900)
    args = ap.parse_args()
    if not args.source:
        _fail("--source (or DINOMEM_WORKSPACE) required")
    res = run_arm(args)
    print(json.dumps({k: v for k, v in res.items() if k != "curve"}, indent=2))
    print("curve:")
    for pt in res["curve"]:
        print(f"  day{pt['through_day']:>3}  overall={pt['overall_accuracy']}  "
              f"stable={pt['stable_accuracy']}  superseding={pt['superseding_accuracy']}")


if __name__ == "__main__":
    main()
