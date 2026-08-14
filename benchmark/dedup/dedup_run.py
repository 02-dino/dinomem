#!/usr/bin/env python3
"""
dedup_run.py — Phase 3 (3b): the dedup/noise drive + measure loop.

Drives ONE arm (rag|base|neuron) over the dedup/noise test set and reports whether
the corpus STAYS COMPACT while KEEPING recall:

  dedup_rate            = 1 - (corpus_items / n_restatements). High = duplicates
                          collapsed. (n_restatements = total duplicate fact turns.)
  corpus_items          = distilled memory items after convergence (absolute size).
  noise_suppression_pct = corpus-size proxy: 1 - min(excess_items, n_noise)/n_noise
                          where excess_items = max(0, corpus_items - n_distinct_facts).
                          HONEST proxy — base/neuron items lack turn provenance, so we
                          cannot attribute an item to a specific noise turn; we report
                          the corpus-size gap AND direct recall so neither axis games.
  recall_retained_pct   = accuracy on the canonical probe questions after the
                          dedup/noise stream (compaction must NOT cost recall).

WHY SIZE + RECALL TOGETHER (anti-gaming): storing nothing => tiny corpus, zero
recall; storing everything => huge corpus, full recall. The capability = SMALL corpus
AND retained recall. Both printed side by side. The RAG floor stores every turn
(corpus_items ~= restatements+noise, dedup_rate ~0). dinomem's dedup gate + cleanup/
review is the mechanism under test.

PROTOCOL: fresh isolated lab -> emit dup+noise haystack -> gold+manifest -> drive to
convergence -> count corpus items -> answer+score canonical probes -> teardown.
Reuses Phase-1 setup_lab/answer/score + Phase-1 drive_base/drive_neuron UNCHANGED.

Usage:
  python3 dedup_run.py --arm rag --corpus <c.json> --source <ws> \\
      [--answer-model M] [--judge-model M] [--stub-judge] \\
      [--overlay-cmd '<neuron install at {ws}>'] [--out results/dedup_<arm>.json]
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
BUILD = HERE / "dedup_build.py"

def _log(m: str) -> None:
    print(f"[dedup_run] {m}", file=sys.stderr)

def _fail(m: str) -> None:
    print(json.dumps({"ok": False, "reason": m}))
    raise SystemExit(f"[dedup_run] FAIL: {m}")

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
    sd = lab_info.get("sessions_dir")
    if sd:
        return sd.rsplit("/sessions", 1)[0]
    return lab_info["lab"]

def _count_corpus_items(arm: str, lab_info: dict) -> int:
    """Count distilled memory items after convergence.
      base/neuron: memory/*.md item files (exclude MEMORY.md + underscore-prefixed
                   pins/notes/history — those aren't distilled-fact items).
      rag: rag_recall.py has NO 'items' abstraction (only index+query); its store
           unit is chunks = one per indexed turn. Count message lines across the
           lab archives = the naive-floor store size (every turn kept)."""
    lab = Path(lab_info["lab"])
    ws = Path(lab_info.get("workspace", str(lab)))
    if arm == "rag":
        n = 0
        for arch in (lab / "sessions").glob("*.jsonl"):
            for line in arch.read_text(encoding="utf-8", errors="replace").splitlines():
                if '"type": "message"' in line or '"type":"message"' in line:
                    n += 1
        return n
    mem = ws / "memory"
    if not mem.exists():
        return 0
    n = 0
    for p in mem.glob("*.md"):
        if p.name == "MEMORY.md" or p.name.startswith("_"):
            continue
        n += 1
    return n

def _drive_arm(arm, lab_info, source, overlay_cmd, timeout) -> dict:
    lab = lab_info["lab"]
    ws = lab_info.get("workspace", lab)
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
        dr = _sh([sys.executable, LM / "drive_neuron.py", "--ws", ws,
                  "--sandbox-root", lab, "--live-source", source,
                  "--live-source-mtime", mtime, "--json"], timeout)
    else:
        dr = _sh([sys.executable, LM / "drive_base.py", "--lab", lab,
                  "--live-source", source, "--live-source-mtime", mtime, "--json"], timeout)
    res = _json_tail(dr.stdout)
    if dr.returncode != 0 or not res.get("ok"):
        _fail(f"{arm} drive did not converge: {res.get('reason')} :: {dr.stderr[-300:]}")
    if arm == "neuron":
        res["recall_cmd"] = (f'{sys.executable} {ws}/tools/hybrid_recall.py "{{q}}" '
                             f'--k {{k}} --json')
    return res

def _probe(arm, lab_info, questions_file, answer_model, judge_model, recall,
           stub_judge, out_prefix, timeout, recall_cmd="") -> dict:
    lab = Path(lab_info["lab"])
    hyp = RESULTS / f"{out_prefix}_hyp.jsonl"
    metrics_out = RESULTS / f"{out_prefix}_metrics.json"
    ans_cmd = [sys.executable, LM / "answer.py", "--lab", lab,
               "--dataset", questions_file, "--out", hyp, "--recall", recall, "--json"]
    # offline CI: stub judge implies stub ANSWER too (answer.py has its own stub
    # path keyed on model=="stub"; passing only score's --stub-judge leaves hyp
    # EMPTY — the Phase-2 bug #3, re-hit in 3a and fixed there too).
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
        return json.loads(Path(metrics_out).read_text())
    except Exception as e:  # noqa: BLE001
        _fail(f"cannot read metrics {metrics_out}: {e}")

def run_arm(args) -> dict:
    RESULTS.mkdir(parents=True, exist_ok=True)
    spec = json.loads(Path(args.corpus).read_text())
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
        em = _sh([sys.executable, BUILD, "emit", "--corpus", args.corpus,
                  "--lab", labroot, "--json"], args.timeout)
        if em.returncode != 0:
            _fail(f"emit failed: {em.stderr[-300:]}")
        qf = RESULTS / f"{args.arm}_dedup_questions.json"
        gd = _sh([sys.executable, BUILD, "gold", "--corpus", args.corpus,
                  "--lab", labroot, "--out", qf], args.timeout)
        if gd.returncode != 0:
            _fail(f"gold failed: {gd.stderr[-300:]}")
        manifest = json.loads(qf.with_suffix(".manifest.json").read_text())

        drive_res = _drive_arm(args.arm, lab_info, args.source, args.overlay_cmd, args.timeout)
        corpus_items = _count_corpus_items(args.arm, lab_info)

        judge = "stub" if args.stub_judge else args.judge_model
        m = _probe(args.arm, lab_info, str(qf), args.answer_model, judge, recall,
                   args.stub_judge, f"{args.arm}_dedup", args.timeout,
                   recall_cmd=drive_res.get("recall_cmd", ""))
    finally:
        if not args.keep_lab:
            _sh([sys.executable, LM / "setup_lab.py", "--teardown", lab_info["lab"]], 120)

    n_rest = manifest["n_restatements"]
    n_facts = manifest["n_distinct_facts"]
    n_noise = manifest["n_noise_turns"]
    dedup_rate = round(1 - (corpus_items / n_rest), 3) if n_rest else None
    excess_items = max(0, corpus_items - n_facts)
    noise_suppression_pct = (round(100 * (1 - min(excess_items, n_noise) / n_noise), 1)
                             if n_noise else None)
    recall_retained = m.get("overall_accuracy")

    result = {
        "ok": True,
        "arm": args.arm,
        "schema": spec["schema"],
        "seed": spec["seed"],
        "answer_model": args.answer_model or ("stub" if args.stub_judge else "gateway-default"),
        "judge_model": "stub" if args.stub_judge else (args.judge_model or "gateway-default"),
        "stub_judge": bool(args.stub_judge),
        "n_distinct_facts": n_facts,
        "n_restatements": n_rest,
        "n_noise_turns": n_noise,
        "corpus_items": corpus_items,
        "ideal_items": n_facts,
        "excess_items": excess_items,
        "dedup_rate": dedup_rate,
        "noise_suppression_pct": noise_suppression_pct,
        "recall_retained_pct": round(100 * recall_retained, 1) if recall_retained is not None else None,
        "note": ("corpus_items for rag = indexed chunks (naive store keeps all); "
                 "noise_suppression is a corpus-size proxy (items lack turn provenance)."),
        "seconds": round(time.time() - t0, 1),
    }
    _log(f"  corpus_items={corpus_items} (ideal {n_facts}) dedup_rate={dedup_rate} "
         f"noise_suppression={noise_suppression_pct}% recall_retained={result['recall_retained_pct']}%")
    out = Path(args.out) if args.out else RESULTS / f"dedup_{args.arm}.json"
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    result["out"] = str(out)
    return result

def main() -> None:
    ap = argparse.ArgumentParser(description="Phase 3b dedup/noise loop (one arm)")
    ap.add_argument("--arm", choices=["rag", "base", "neuron"], default="rag")
    ap.add_argument("--corpus", required=True, help="corpus.json from dedup_build")
    ap.add_argument("--source", default=os.environ.get("DINOMEM_WORKSPACE", ""))
    ap.add_argument("--answer-model", default=os.environ.get("DINOMEM_BENCH_ANSWER_MODEL", ""))
    ap.add_argument("--judge-model", default=os.environ.get("DINOMEM_BENCH_JUDGE_MODEL", ""))
    ap.add_argument("--recall", choices=["base", "command"], default=None)
    ap.add_argument("--overlay-cmd", default=os.environ.get("DINOMEM_BENCH_OVERLAY_CMD", ""))
    ap.add_argument("--stub-judge", action="store_true",
                    help="offline CI: deterministic, no spend, non-canonical")
    ap.add_argument("--out", default="")
    ap.add_argument("--keep-lab", action="store_true")
    ap.add_argument("--timeout", type=int, default=900)
    args = ap.parse_args()
    if not args.source:
        _fail("--source (or DINOMEM_WORKSPACE) required")
    res = run_arm(args)
    print(json.dumps(res, indent=2))

if __name__ == "__main__":
    main()
