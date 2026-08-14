#!/usr/bin/env python3
"""
promotion_run.py — Phase 4 (4b): promotion precision/recall/false-promotion loop.

Drives ONE arm over the labeled reliable/unreliable fact set and, for the NEURON
arm, evaluates the L4 promoter (memory_promote.py -> promotion_state.json) as a
CLASSIFIER against ground-truth labels:

  promotion_precision  = promoted ∩ reliable / promoted        (few false promotes)
  promotion_recall     = promoted ∩ reliable / all reliable    (catches real ones)
  false_promotion_rate = promoted ∩ unreliable / promoted      (the danger metric)
  recall_retained_pct  = accuracy on the reliable-fact recall probes (a promoted
                         fact must STAY retrievable — promotion that loses recall is
                         worthless).

JOIN METHOD: promotion_state.json entries with status=="trusted" are the PROMOTED
set. Each entry's text surface (pattern + rule_line + sources) is matched against
each labeled subject's ANSWER/keywords to attribute a promotion to a subject. This
is a keyword-attribution (the promoter keys by its own pattern hash, not our
subject id), so we report the matched counts AND the raw trusted-entry list for
auditability. Unmatched trusted entries are flagged (neither reliable nor
unreliable keyword hit) so precision isn't silently inflated.

RAG + base arms have NO promotion tier -> reported as promotion_supported=false
(promotion_state absent). Not a failure — only neuron has L4. Their recall_retained
still runs (shows base/RAG keep the fact without a promotion concept).

PROTOCOL mirrors the other Phase-3/4 runners. Offline note: the neuron promoter
needs a real extract model (same paid-only constraint as base/neuron everywhere);
the RAG arm runs at $0 with --stub-judge but has no promotion layer to measure.

Usage:
  python3 promotion_run.py --arm neuron --facts <f.json> --source <ws> \\
      --answer-model M --judge-model M \\
      --overlay-cmd '<neuron install at {ws}>' [--out results/promotion_<arm>.json]
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
BUILD = HERE / "promotion_build.py"

def _log(m): print(f"[promotion_run] {m}", file=sys.stderr)
def _fail(m):
    print(json.dumps({"ok": False, "reason": m}))
    raise SystemExit(f"[promotion_run] FAIL: {m}")

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

def _read_promotion_state(lab_info):
    """Return the list of TRUSTED (promoted) entries from the lab's promotion_state.
    Path mirrors memory_promote.py: <BASE>/kb/memory_neuron/l4_promoted/promotion_state.json
    where BASE = the workspace (DINOMEM_WORKSPACE) = lab_info['workspace']."""
    ws = Path(lab_info.get("workspace", lab_info["lab"]))
    sf = ws / "kb" / "memory_neuron" / "l4_promoted" / "promotion_state.json"
    if not sf.exists():
        return None  # no promotion layer materialized
    try:
        state = json.loads(sf.read_text())
    except Exception:  # noqa: BLE001
        return None
    trusted = []
    for k, e in state.items():
        if isinstance(e, dict) and e.get("status") == "trusted":
            surface = " ".join(str(e.get(f, "")) for f in ("pattern", "rule_line"))
            srcs = e.get("sources") or []
            surface += " " + " ".join(str(s) for s in srcs)
            # reinforce_count at trusted = promotion LATENCY (how much evidence before it
            # graduated). memory_promote graduates at GRADUATE_REINFORCE reinforcing runs.
            trusted.append({"key": k, "surface": surface.lower(),
                            "reinforce_count": e.get("reinforce_count")})
    return trusted

def _read_retired(lab_info):
    """Read the demoted/retired insight archive (memory/_history/demoted_insights.jsonl).
    memory_promote.retire_insight() appends here on every expire/demote (trusted ->
    invalidated). Returns list of {surface, reason} or [] if none."""
    ws = Path(lab_info.get("workspace", lab_info["lab"]))
    hf = ws / "memory" / "_history" / "demoted_insights.jsonl"
    if not hf.exists():
        return []
    out = []
    try:
        for line in hf.read_text().splitlines():
            if not line.strip():
                continue
            e = json.loads(line)
            surf = " ".join(str(e.get(f, "")) for f in ("pattern", "rule_line")).lower()
            out.append({"surface": surf, "reason": e.get("reason", "")})
    except Exception:  # noqa: BLE001
        return out
    return out

def _classify_promotions(trusted, reliable_subjects, unreliable_subjects, facts_spec):
    """Attribute each trusted entry to reliable / unreliable / unmatched by keyword.
    Keywords per subject = the fact answer + subject leaf token + statement nouns."""
    # build keyword sets per subject from the spec
    kw = {}
    for f in facts_spec["reliable"] + facts_spec["unreliable"]:
        toks = set()
        leaf = f["subject"].split(".")[-1]
        toks.add(leaf.lower())
        if f.get("answer"):
            for w in re.findall(r"[a-z0-9]+", f["answer"].lower()):
                if len(w) > 2:
                    toks.add(w)
        # a couple distinctive words from the first statement
        for w in re.findall(r"[a-z0-9]+", " ".join(f["emit_statements"]).lower()):
            if len(w) > 4:
                toks.add(w)
        kw[f["subject"]] = toks
    rel_set = set(reliable_subjects); unrel_set = set(unreliable_subjects)
    promoted_reliable, promoted_unreliable, unmatched = set(), set(), []
    for t in trusted:
        matched_subj = None; best = 0
        for subj, toks in kw.items():
            hits = sum(1 for w in toks if w in t["surface"])
            if hits > best:
                best = hits; matched_subj = subj
        if best == 0 or matched_subj is None:
            unmatched.append(t["key"])
        elif matched_subj in rel_set:
            promoted_reliable.add(matched_subj)
        elif matched_subj in unrel_set:
            promoted_unreliable.add(matched_subj)
        else:
            unmatched.append(t["key"])
    return promoted_reliable, promoted_unreliable, unmatched

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
        return json.loads(Path(metrics_out).read_text())
    except Exception as e:  # noqa: BLE001
        _fail(f"cannot read metrics {metrics_out}: {e}")

def run_arm(args):
    RESULTS.mkdir(parents=True, exist_ok=True)
    spec = json.loads(Path(args.facts).read_text())
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
        em = _sh([sys.executable, BUILD, "emit", "--facts", args.facts, "--lab", labroot, "--json"], args.timeout)
        if em.returncode != 0:
            _fail(f"emit failed: {em.stderr[-300:]}")
        qf = RESULTS / f"{args.arm}_promotion_questions.json"
        gd = _sh([sys.executable, BUILD, "gold", "--facts", args.facts, "--lab", labroot, "--out", qf], args.timeout)
        if gd.returncode != 0:
            _fail(f"gold failed: {gd.stderr[-300:]}")
        manifest = json.loads(qf.with_suffix(".manifest.json").read_text())
        drive_res = _drive_arm(args.arm, lab_info, args.source, args.overlay_cmd, args.timeout)
        trusted = _read_promotion_state(lab_info) if args.arm == "neuron" else None
        judge = "stub" if args.stub_judge else args.judge_model
        m = _probe(lab_info, str(qf), args.answer_model, judge, recall, args.stub_judge,
                   f"{args.arm}_promotion", args.timeout, recall_cmd=drive_res.get("recall_cmd", ""))
    finally:
        if not args.keep_lab:
            _sh([sys.executable, LM / "setup_lab.py", "--teardown", lab_info["lab"]], 120)

    recall_retained = m.get("overall_accuracy")
    result = {"ok": True, "arm": args.arm, "schema": spec["schema"], "seed": spec["seed"],
              "answer_model": args.answer_model or ("stub" if args.stub_judge else "gateway-default"),
              "judge_model": "stub" if args.stub_judge else (args.judge_model or "gateway-default"),
              "stub_judge": bool(args.stub_judge),
              "n_reliable": manifest["n_reliable"], "n_unreliable": manifest["n_unreliable"],
              "recall_retained_pct": round(100 * recall_retained, 1) if recall_retained is not None else None}

    if trusted is None:
        result.update({"promotion_supported": False,
                       "note": "arm has no L4 promotion layer (only neuron does); "
                               "promotion metrics N/A, recall_retained still reported."})
    else:
        pr, pu, unmatched = _classify_promotions(
            trusted, manifest["reliable_subjects"], manifest["unreliable_subjects"], spec)
        n_promoted_attributed = len(pr) + len(pu)
        precision = round(len(pr) / n_promoted_attributed, 3) if n_promoted_attributed else None
        recall_m = round(len(pr) / manifest["n_reliable"], 3) if manifest["n_reliable"] else None
        false_rate = round(len(pu) / n_promoted_attributed, 3) if n_promoted_attributed else None
        # promotion_latency = mean reinforce_count among trusted entries (evidence
        # before graduation). memory_promote graduates at GRADUATE_REINFORCE; a
        # higher mean = the gate demanded more corroboration before trusting. PDF §6C.
        rc = [t["reinforce_count"] for t in trusted if isinstance(t.get("reinforce_count"), (int, float))]
        promotion_latency = round(sum(rc) / len(rc), 2) if rc else None
        # retirement_accuracy = of the UNRELIABLE facts that got (wrongly) trusted,
        # fraction later retired/demoted (appear in demoted_insights archive). Measures
        # whether invalidated knowledge stops influencing behavior. PDF §6C.
        retired = _read_retired(lab_info)
        retired_surfaces = " ".join(r["surface"] for r in retired)
        # attribute retirements to the unreliable subjects (same keyword join used above)
        n_unrel_trusted = len(pu)
        n_unrel_retired = 0
        for subj in pu:
            leaf = subj.split(".")[-1].lower()
            if leaf and leaf in retired_surfaces:
                n_unrel_retired += 1
        retirement_accuracy = (round(100 * n_unrel_retired / n_unrel_trusted, 1)
                               if n_unrel_trusted else None)
        result.update({
            "promotion_supported": True,
            "n_trusted_entries": len(trusted),
            "promoted_reliable": sorted(pr), "promoted_unreliable": sorted(pu),
            "unmatched_trusted_keys": unmatched,
            "promotion_precision": precision,
            "promotion_recall": recall_m,
            "false_promotion_rate": false_rate,
            "promotion_latency": promotion_latency,          # PDF §6C: evidence before trust
            "n_retired": len(retired),
            "retirement_accuracy": retirement_accuracy,      # PDF §6C: invalid knowledge retired
            "note": "promotion attributed to subjects by keyword; unmatched_trusted_keys "
                    "are trusted entries hitting no labeled subject (audit). "
                    "promotion_latency=mean reinforce_count at trust; retirement_accuracy="
                    "unreliable-trusted entries later demoted (may be null if none wrongly trusted).",
        })
        _log(f"  precision={precision} recall={recall_m} false_promotion={false_rate} "
             f"latency={promotion_latency} retirement_acc={retirement_accuracy} "
             f"recall_retained={result['recall_retained_pct']}%")

    out = Path(args.out) if args.out else RESULTS / f"promotion_{args.arm}.json"
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    result["out"] = str(out)
    return result

def main():
    ap = argparse.ArgumentParser(description="Phase 4b promotion precision/recall loop (one arm)")
    ap.add_argument("--arm", choices=["rag", "base", "neuron"], default="neuron")
    ap.add_argument("--facts", required=True)
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
