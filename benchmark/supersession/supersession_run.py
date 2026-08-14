#!/usr/bin/env python3
"""
supersession_run.py — Phase 3 (3a): the supersession/retention drive + probe loop.

Drives ONE arm (rag|base|neuron) over the supersession test set and reports the
two Phase-3a metrics the strategy PDF demands:

  supersession_correct_pct  — of superseded subjects, fraction whose NOW-query
                              returns the CURRENT value (replaced old truth wins).
  history_recovery_pct      — of superseded subjects, fraction whose AS-OF-PAST
                              query returns the correct HISTORICAL value (old truth
                              preserved, not deleted).

WHY BOTH MATTER: a system can score high on ONE and fail the other.
  * Delete-on-supersede would ace supersession_correct but zero history_recovery.
  * A naive RAG floor keeps everything but can't rank current-vs-stale, so it fails
    supersession_correct (Phase-2 already showed the 3-way similarity tie), and it
    has no as-of notion so history_recovery is chance.
  * Neuron's valid_time (retire-to-_history + valid_from/valid_until + is_valid_at)
    is designed to pass BOTH — current truth replaces old WHILE preserving history.

PROTOCOL (single haystack, two probe sets, one drive):
  1. fresh isolated lab (Phase-1 setup_lab.py; layout=real for neuron else flat).
  2. emit the whole value-chain haystack (supersession_build emit).
  3. write NOW + PAST gold + question file (supersession_build gold).
  4. drive the arm to convergence (rag=index; base/neuron=real front door).
     For NEURON, the drive materializes valid_time supersession (dedup gate ->
     supersede -> retire) so _history/ is populated — that IS the mechanism tested.
  5. answer + score (reuse Phase-1 answer.py + score.py UNCHANGED). The neuron arm
     passes each probe's `as_of` into hybrid_recall via --as-of (bitemporal read).
  6. split graded results by kind (now|past) -> the two headline percentages,
     plus a stable-control check (must be correct at both now and past).
  7. teardown.

Reuses the Phase-1 harness modules by path (ONE shared protocol). Models selectable
(same env/flags). --stub-judge for offline CI (deterministic, $0, non-canonical).

Usage:
  python3 supersession_run.py --arm rag --subjects <s.json> --source <ws> \\
      [--answer-model M] [--judge-model M] [--stub-judge] \\
      [--overlay-cmd '<neuron install at {ws}>'] [--out results/supersession_<arm>.json]
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
LM = HERE.parent / "longmemeval"          # Phase-1 harness modules
RESULTS = HERE / "results"
BUILD = HERE / "supersession_build.py"

def _log(m: str) -> None:
    print(f"[supersession_run] {m}", file=sys.stderr)

def _fail(m: str) -> None:
    print(json.dumps({"ok": False, "reason": m}))
    raise SystemExit(f"[supersession_run] FAIL: {m}")

def _sh(cmd, timeout: int, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([str(c) for c in cmd], capture_output=True,
                          text=True, timeout=timeout, env=env)

def _json_tail(s: str) -> dict:
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

def _labroot(lab_info: dict) -> str:
    """The parent-of-sessions dir the builder writes into. For layout=real the
    sessions live at <sessions_dir>; for flat at <lab>/sessions. Derive the parent
    the same way the longitudinal runner does."""
    sd = lab_info.get("sessions_dir")
    if sd:
        return sd.rsplit("/sessions", 1)[0]
    return lab_info["lab"]

def _drive_arm(arm: str, lab_info: dict, source: str, overlay_cmd: str,
               timeout: int) -> dict:
    """Drive the arm's pipeline to convergence (mirrors run.py / longitudinal_run)."""
    lab = lab_info["lab"]
    ws = lab_info.get("workspace", lab)
    mtime = lab_info["live_source_mtime"]
    if arm == "rag":
        ir = _sh([sys.executable, LM / "rag_recall.py", "index", "--lab", lab], timeout)
        if ir.returncode != 0:
            _fail(f"rag index failed: {ir.stderr[-300:]}")
        recall_cmd = (f'{sys.executable} {LM / "rag_recall.py"} query '
                      f'--lab {lab} "{{q}}" --k {{k}} --json')
        return {"ok": True, "arm": "rag", "note": "no-pipeline", "recall_cmd": recall_cmd}
    if arm == "neuron":
        if not overlay_cmd:
            _fail("--arm neuron requires --overlay-cmd (neuron overlay onto the lab WS)")
        ov = _sh(["bash", "-c", overlay_cmd.replace("{ws}", ws).replace("{lab}", lab)], timeout)
        if ov.returncode != 0:
            _fail(f"neuron overlay failed: {ov.stderr[-300:]}")
        _dn = [sys.executable, LM / "drive_neuron.py", "--ws", ws,
               "--sandbox-root", lab, "--live-source", source,
               "--live-source-mtime", mtime, "--json"]
        for _sk in filter(None, os.environ.get("DINOMEM_BENCH_SKIP_STAGE", "").split(",")):
            _dn += ["--skip-stage", _sk]   # Phase 5b ablation forward
        dr = _sh(_dn, timeout)
        # neuron recall = hybrid_recall with per-probe --as-of (bitemporal). answer.py
        # substitutes {as_of} if present in the recall command (see below).
        recall_cmd = (f'{sys.executable} {ws}/tools/hybrid_recall.py "{{q}}" '
                      f'--k {{k}} --json --as-of {{as_of}}')
    else:
        dr = _sh([sys.executable, LM / "drive_base.py", "--lab", lab,
                  "--live-source", source, "--live-source-mtime", mtime, "--json"], timeout)
        recall_cmd = ""
    res = _json_tail(dr.stdout)
    if dr.returncode != 0 or not res.get("ok"):
        _fail(f"{arm} drive did not converge: {res.get('reason')} :: {dr.stderr[-300:]}")
    if recall_cmd:
        res["recall_cmd"] = recall_cmd
    return res

def _probe(arm, lab_info, questions_file, answer_model, judge_model, recall,
           stub_judge, out_prefix, timeout, recall_cmd="") -> tuple[dict, Path]:
    """answer + score. Reuses Phase-1 answer.py + score.py UNCHANGED. Returns
    (metrics_dict, hyp_path)."""
    lab = Path(lab_info["lab"])
    hyp = RESULTS / f"{out_prefix}_hyp.jsonl"
    metrics_out = RESULTS / f"{out_prefix}_metrics.json"
    ans_cmd = [sys.executable, LM / "answer.py", "--lab", lab,
               "--dataset", questions_file, "--out", hyp, "--recall", recall, "--json"]
    # offline CI: stub judge implies stub ANSWER too (mirrors longitudinal_run) so
    # the whole recall->answer->score loop runs at $0 with zero gateway calls.
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
    if stub_judge:
        score_cmd += ["--judge", "stub"]
    elif judge_model:
        score_cmd += ["--judge", judge_model]
    sr = _sh(score_cmd, timeout)
    if sr.returncode != 0:
        _fail(f"score.py failed: {sr.stderr[-300:]}")
    try:
        return json.loads(Path(metrics_out).read_text()), hyp
    except Exception as e:  # noqa: BLE001
        _fail(f"cannot read metrics {metrics_out}: {e}")

def _split_by_kind(hyp_path: Path, ref: dict) -> dict:
    """From the graded eval-results, split accuracy by probe kind (now|past) and
    isolate the two Phase-3a headline metrics + the stable control.
      supersession_correct = accuracy on NOW probes of SUPERSEDED subjects.
      history_recovery     = accuracy on PAST probes (all PAST probes are on
                             superseded subjects by construction).
      stable_control       = accuracy on NOW probes of NON-superseded subjects
                             (must be ~1.0; guards against over-retiring)."""
    cand = list(hyp_path.parent.glob(hyp_path.name + ".eval-results-*"))
    if not cand:
        return {"note": "no eval-results file"}
    now_superseded, now_stable, past = [], [], []
    for line in cand[0].read_text().splitlines():
        if not line.strip():
            continue
        e = json.loads(line)
        qid = e.get("question_id")
        lab = 1 if (e.get("autoeval_label") or {}).get("label") else 0
        r = ref.get(qid) or {}
        qt = r.get("question_type", "")
        # superseded flag comes from the question file we pass as ref (answer.py
        # question array). We stored `subject`; superseded-ness is derivable from
        # whether the subject has any PAST probe. Simpler: NOW probe id ends __now.
        if qt == "supersession-past":
            past.append(lab)
        elif qt == "supersession-now":
            subj = r.get("subject", "")
            # a subject is superseded iff it also has a past probe in ref
            has_past = any(
                (v.get("subject") == subj and v.get("question_type") == "supersession-past")
                for v in ref.values())
            (now_superseded if has_past else now_stable).append(lab)
    def _pct(v):
        return round(100 * sum(v) / len(v), 1) if v else None
    return {
        "supersession_correct_pct": _pct(now_superseded),
        "supersession_correct_n": len(now_superseded),
        "history_recovery_pct": _pct(past),
        "history_recovery_n": len(past),
        "stable_control_pct": _pct(now_stable),
        "stable_control_n": len(now_stable),
    }

def run_arm(args) -> dict:
    RESULTS.mkdir(parents=True, exist_ok=True)
    spec = json.loads(Path(args.subjects).read_text())
    recall = args.recall or ("base" if args.arm == "base" else "command")
    layout = "real" if args.arm == "neuron" else "flat"
    t0 = time.time()

    # 1. fresh isolated lab
    setup = _sh([sys.executable, LM / "setup_lab.py", "--source", args.source,
                 "--layout", layout, "--json"], args.timeout)
    if setup.returncode != 0:
        _fail(f"setup_lab failed: {setup.stderr[-300:]}")
    lab_info = _json_tail(setup.stdout)
    labroot = _labroot(lab_info)
    try:
        # 2. emit haystack
        em = _sh([sys.executable, BUILD, "emit", "--subjects", args.subjects,
                  "--lab", labroot, "--json"], args.timeout)
        if em.returncode != 0:
            _fail(f"emit failed: {em.stderr[-300:]}")
        # 3. NOW + PAST gold + question file
        qf = RESULTS / f"{args.arm}_supersession_questions.json"
        gd = _sh([sys.executable, BUILD, "gold", "--subjects", args.subjects,
                  "--lab", labroot, "--out", qf], args.timeout)
        if gd.returncode != 0:
            _fail(f"gold failed: {gd.stderr[-300:]}")
        # 4. drive
        drive_res = _drive_arm(args.arm, lab_info, args.source, args.overlay_cmd,
                               args.timeout)
        # 5. answer + score
        judge = "stub" if args.stub_judge else args.judge_model
        m, hyp = _probe(args.arm, lab_info, str(qf), args.answer_model, judge,
                        recall, args.stub_judge, f"{args.arm}_supersession",
                        args.timeout, recall_cmd=drive_res.get("recall_cmd", ""))
        # 6. split by kind
        ref = {e["question_id"]: e for e in json.loads(qf.read_text())}
        split = _split_by_kind(hyp, ref)
        _log(f"  supersession_correct={split.get('supersession_correct_pct')}% "
             f"history_recovery={split.get('history_recovery_pct')}% "
             f"stable_control={split.get('stable_control_pct')}%")
    finally:
        if not args.keep_lab:
            _sh([sys.executable, LM / "setup_lab.py", "--teardown", lab_info["lab"]], 120)

    result = {
        "ok": True,
        "arm": args.arm,
        "schema": spec["schema"],
        "seed": spec["seed"],
        "answer_model": args.answer_model or "gateway-default",
        "judge_model": "stub" if args.stub_judge else (args.judge_model or "gateway-default"),
        "stub_judge": bool(args.stub_judge),
        "n_subjects": spec["n_subjects"],
        "n_superseded": spec["n_superseded"],
        "overall_accuracy": m.get("overall_accuracy"),
        "supersession_correct_pct": split.get("supersession_correct_pct"),
        "supersession_correct_n": split.get("supersession_correct_n"),
        "history_recovery_pct": split.get("history_recovery_pct"),
        "history_recovery_n": split.get("history_recovery_n"),
        "stable_control_pct": split.get("stable_control_pct"),
        "stable_control_n": split.get("stable_control_n"),
        "seconds": round(time.time() - t0, 1),
    }
    out = Path(args.out) if args.out else RESULTS / f"supersession_{args.arm}.json"
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    result["out"] = str(out)
    return result

def main() -> None:
    ap = argparse.ArgumentParser(description="Phase 3a supersession/retention loop (one arm)")
    ap.add_argument("--arm", choices=["rag", "base", "neuron"], default="rag")
    ap.add_argument("--subjects", required=True, help="subjects.json from supersession_build")
    ap.add_argument("--source", default=os.environ.get("DINOMEM_WORKSPACE", ""),
                    help="installed dinomem workspace (source of procedures/tools)")
    ap.add_argument("--answer-model", default=os.environ.get("DINOMEM_BENCH_ANSWER_MODEL", ""))
    ap.add_argument("--judge-model", default=os.environ.get("DINOMEM_BENCH_JUDGE_MODEL", ""))
    ap.add_argument("--recall", choices=["base", "command"], default=None)
    ap.add_argument("--overlay-cmd", default=os.environ.get("DINOMEM_BENCH_OVERLAY_CMD", ""))
    ap.add_argument("--stub-judge", action="store_true",
                    help="offline CI: deterministic substring grader, no spend, non-canonical")
    ap.add_argument("--out", default="")
    ap.add_argument("--keep-lab", action="store_true")
    ap.add_argument("--timeout", type=int, default=900)
    args = ap.parse_args()
    if not args.source:
        _fail("--source (or DINOMEM_WORKSPACE) required")
    res = run_arm(args)
    print(json.dumps({k: v for k, v in res.items()}, indent=2))

if __name__ == "__main__":
    main()
