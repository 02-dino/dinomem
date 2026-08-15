#!/usr/bin/env python3
"""
score.py — OFFICIAL LongMemEval scoring, gateway-routed.

Grades a hypothesis JSONL (from answer.py, official {question_id, hypothesis} shape)
against the dataset's gold answers, using LongMemEval's OWN judge prompt
(get_anscheck_prompt, imported UNCHANGED from vendor/longmemeval/evaluate_qa.py)
and the official decode determinism (temperature=0, max_tokens=10,
label = "yes" in response.lower()).

WHY NOT just run the upstream evaluate_qa.py directly?
  The upstream script calls the OpenAI SDK against api.openai.com and only accepts
  3 judge ids (gpt-4o / gpt-4o-mini / llama-3.1-70b). dinomem ships to users on
  arbitrary providers, so we route the SAME judge prompt through the user's
  OpenClaw gateway instead. ONLY THE TRANSPORT CHANGES — the grading prompt and
  temperature=0 determinism are byte-identical to official, which is what keeps
  the number comparable. (For a strictly-canonical OpenAI run, set --canonical to
  shell the vendored upstream evaluate_qa.py with OPENAI_API_KEY instead.)

CANONICAL-JUDGE STAMP:
  print_qa_metrics.py hard-asserts autoeval_label.model == 'gpt-4o-2024-08-06'.
  When --judge resolves to a gpt-4o-class model we stamp that canonical id so the
  upstream aggregator accepts our labels; otherwise we stamp the real model id and
  mark the run non-canonical (aggregation still works via our own equivalent math,
  clearly labeled).

OUTPUT:
  <hyp>.eval-results-<judge>  : per-line entry + autoeval_label (upstream-compatible)
  --metrics-out <path.json>   : {overall, task_averaged, per_category{...}, abstention,
                                 judge_model, canonical, n_graded}
CATEGORIES (fixed, official 6): single-session-user, single-session-assistant,
  single-session-preference, temporal-reasoning, knowledge-update, multi-session.
  Abstention = question_id endswith '_abs' (graded, reported separately too).

USAGE:
  python3 score.py --hyp <hypotheses.jsonl> --ref <dataset.json> \\
      [--judge <model>] [--metrics-out results/base_metrics.json] \\
      [--canonical] [--timeout 60] [--json]
"""
import argparse
import json
import os
import subprocess
import sys
import shutil
from pathlib import Path

# Import the OFFICIAL judge prompt UNCHANGED (load-bearing for comparability).
_VENDOR = Path(__file__).parent / "vendor" / "longmemeval"
sys.path.insert(0, str(_VENDOR))
try:
    from evaluate_qa import get_anscheck_prompt  # type: ignore
except Exception as e:  # noqa: BLE001
    print(f"[score] FAIL: cannot import official get_anscheck_prompt from {_VENDOR}: {e}",
          file=sys.stderr)
    sys.exit(1)

CANONICAL_JUDGE_ID = "gpt-4o-2024-08-06"
CATEGORIES = [
    "single-session-user", "single-session-preference", "single-session-assistant",
    "multi-session", "temporal-reasoning", "knowledge-update",
]


def _fail(msg: str, code: int = 1):
    print(f"[score] FAIL: {msg}", file=sys.stderr)
    sys.exit(code)


def _log(msg: str):
    print(f"[score] {msg}", file=sys.stderr)


def _resolve_openclaw() -> str:
    return shutil.which("openclaw") or "/home/linuxbrew/.linuxbrew/bin/openclaw"


def _is_gpt4o_class(model: str) -> bool:
    m = (model or "").lower()
    return "gpt-4o" in m or "gpt4o" in m or m.endswith("/gpt-4o") or "4o" in m.split("-")[-1:]


def judge_via_gateway(prompt: str, model: str, timeout: int) -> tuple[bool, str]:
    """Route the official judge prompt through the OpenClaw gateway.
    Returns (label_bool, raw_response). Fail-loud on transport error (caller counts).

    NOTE: `openclaw infer model run` exposes NO decode knobs (--temperature /
    --max-tokens are NOT accepted — passing them makes the CLI reject the whole
    call, which is bug #7: all judge calls errored). The judge prompt is a strict
    yes/no classification, so gateway defaults are fine; the label parse only
    looks for 'yes'. Determinism note stays in the module docstring as intent,
    but the CLI gives us no per-call temperature control here.
    """
    cmd = [_resolve_openclaw(), "infer", "model", "run",
           "--prompt", prompt, "--gateway", "--json"]
    if model:
        cmd += ["--model", model]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode == 0:
            raw = r.stdout
            start = raw.find("{")
            obj = json.loads(raw[start:] if start != -1 else raw)
            if obj.get("ok") and obj.get("outputs"):
                text = (obj["outputs"][0].get("text") or "").strip()
                return ("yes" in text.lower()), text
        return False, f"__ERROR__ {(r.stderr or '')[:160]}"
    except subprocess.TimeoutExpired:
        return False, "__ERROR__ timeout"
    except Exception as e:  # noqa: BLE001
        return False, f"__ERROR__ {str(e)[:160]}"


def load_hypotheses(path: Path) -> list[dict]:
    try:
        return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    except Exception:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            _fail(f"cannot read hypotheses {path}: {e}")


def load_reference(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    return {e["question_id"]: e for e in data}


def run_canonical_upstream(hyp: Path, ref: Path, judge_short: str, timeout: int) -> int:
    """Shell the vendored upstream evaluate_qa.py verbatim (OpenAI transport).
    Requires OPENAI_API_KEY. Returns the subprocess return code.
    """
    if not os.environ.get("OPENAI_API_KEY"):
        _fail("--canonical needs OPENAI_API_KEY set (upstream evaluate_qa.py uses the OpenAI SDK)")
    script = _VENDOR / "evaluate_qa.py"
    _log(f"canonical mode: shelling upstream {script} judge={judge_short}")
    r = subprocess.run([sys.executable, str(script), judge_short, str(hyp), str(ref)],
                       timeout=timeout)
    return r.returncode


# ── 1b: retrieval + cost + latency + storage metrics ─────────────────────────
def _load_retrieval_log(path: Path) -> dict:
    """answer.py telemetry sidecar keyed by question_id."""
    out = {}
    if not path or not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        qid = r.get("question_id")
        if qid:
            out[qid] = r
    return out


def _load_gold(gold_dir: Path) -> dict:
    """adapter <qid>.gold.json sidecars keyed by question_id.
    Each: {answer_session_ids:[...], emitted_session_ids:[...]}."""
    out = {}
    if not gold_dir or not gold_dir.is_dir():
        return out
    for gf in gold_dir.glob("*.gold.json"):
        try:
            g = json.loads(gf.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        qid = g.get("question_id")
        if qid:
            out[qid] = g
    return out


def _dir_size_bytes(path: Path) -> int:
    total = 0
    if not path or not path.exists():
        return 0
    for p in path.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


def compute_aux_metrics(rlog: dict, gold: dict, lab: Path | None) -> dict:
    """Retrieval recall/precision (vs gold answer-sessions) + cost/latency/storage.

    Retrieval metrics are computed ONLY over questions where BOTH gold
    answer_session_ids AND the engine's retrieved_session_ids are present — an
    engine that surfaces no session attribution (e.g. base recall over distilled
    memory items, which are file-sourced not session-sourced) reports
    retrieval_attributable=0 and null recall/precision, honestly rather than
    fabricating. That asymmetry is itself a finding: RAG/neuron retrieve
    session-attributable evidence; base retrieves distilled items.
    """
    recalls, precisions = [], []
    attributable = 0
    retrieved_counts, ctx_chars = [], []
    prompt_toks, completion_toks, total_toks = [], [], []
    recall_ms, answer_ms = [], []

    for qid, r in rlog.items():
        retrieved_counts.append(r.get("retrieved_count", 0))
        if r.get("context_chars") is not None:
            ctx_chars.append(r["context_chars"])
        for acc, key in ((prompt_toks, "prompt_tokens"),
                         (completion_toks, "completion_tokens"),
                         (total_toks, "total_tokens")):
            v = r.get(key)
            if isinstance(v, (int, float)):
                acc.append(v)
        if isinstance(r.get("recall_ms"), (int, float)):
            recall_ms.append(r["recall_ms"])
        if isinstance(r.get("answer_ms"), (int, float)):
            answer_ms.append(r["answer_ms"])

        g = gold.get(qid)
        # DUAL-GRANULARITY gold: LoCoMo ships turn-level evidence dia_ids (finer,
        # native), LongMemEval ships session-level answer_session_ids. Prefer the
        # finest gold BOTH sides can express: if the gold has evidence_dia_ids AND
        # the engine surfaced dia_ids, measure at TURN granularity; else fall back
        # to session granularity. This makes the LoCoMo metric honest (adversarial
        # Qs with empty evidence are excluded from recall/precision by the
        # `want`-nonempty gate, exactly as they should be).
        want_dia = set(str(x) for x in (g.get("evidence_dia_ids") if g else []) or [])
        got_dia = set(str(x) for x in (r.get("retrieved_dia_ids") or []))
        if want_dia and got_dia:
            granularity = "turn"
            want, got = want_dia, got_dia
        else:
            granularity = "session"
            got = set(str(x) for x in (r.get("retrieved_session_ids") or []))
            want = set(str(x) for x in (g.get("answer_session_ids") if g else []) or [])
        if want and got:
            attributable += 1
            hit = got & want
            recalls.append(len(hit) / len(want))
            precisions.append(len(hit) / len(got))

    def _avg(v):
        return round(sum(v) / len(v), 4) if v else None
    def _sum(v):
        return int(sum(v)) if v else None

    storage_bytes = None
    storage_breakdown = {}
    if lab and lab.exists():
        mem = lab / "memory"
        idx = lab / ".rag_index"
        storage_breakdown = {
            "memory_bytes": _dir_size_bytes(mem),
            "rag_index_bytes": _dir_size_bytes(idx),
        }
        storage_bytes = storage_breakdown["memory_bytes"] + storage_breakdown["rag_index_bytes"]

    return {
        "retrieval": {
            "attributable_questions": attributable,
            "recall_at_k": _avg(recalls),
            "precision_at_k": _avg(precisions),
            "avg_retrieved_count": _avg(retrieved_counts),
            "note": ("recall/precision over questions with BOTH gold answer_session_ids "
                     "and engine session attribution; null = engine surfaces no "
                     "session-level attribution (e.g. base distilled-memory recall)"),
        },
        "cost": {
            "avg_context_chars": _avg(ctx_chars),
            "avg_prompt_tokens": _avg(prompt_toks),
            "avg_completion_tokens": _avg(completion_toks),
            "avg_total_tokens": _avg(total_toks),
            "sum_total_tokens": _sum(total_toks),
            "tokens_measured": len(total_toks),
        },
        "latency": {
            "avg_recall_ms": _avg(recall_ms),
            "avg_answer_ms": _avg(answer_ms),
        },
        "storage": {
            "total_bytes": storage_bytes,
            **storage_breakdown,
        },
    }


def main():
    ap = argparse.ArgumentParser(description="Official LongMemEval scoring, gateway-routed.")
    ap.add_argument("--hyp", required=True, help="hypotheses.jsonl ({question_id,hypothesis})")
    ap.add_argument("--ref", required=True, help="dataset json (gold answers + question_type)")
    ap.add_argument("--judge", default=os.environ.get("DINOMEM_BENCH_JUDGE_MODEL", ""),
                    help="judge model (default env DINOMEM_BENCH_JUDGE_MODEL; recommend gpt-4o-class)")
    ap.add_argument("--metrics-out", help="write metrics JSON here")
    ap.add_argument("--results-out", help="write per-line eval-results JSONL here "
                    "(default: <hyp>.eval-results-<judge>)")
    ap.add_argument("--canonical", action="store_true",
                    help="shell upstream evaluate_qa.py via OpenAI SDK (needs OPENAI_API_KEY)")
    ap.add_argument("--judge-short", default="gpt-4o",
                    help="short judge id for --canonical (gpt-4o|gpt-4o-mini|llama-3.1-70b-instruct)")
    ap.add_argument("--timeout", type=int, default=60)
    # 1b: full metric set inputs (all optional; metrics degrade gracefully if absent)
    ap.add_argument("--retrieval-log", help="answer.py <hyp>.retrieval.jsonl telemetry "
                    "(retrieved_session_ids, tokens, latency). Default: <hyp>.retrieval.jsonl")
    ap.add_argument("--gold-dir", help="dir of <qid>.gold.json sidecars (adapter emit) for "
                    "retrieval recall/precision. Default: <lab>/sessions")
    ap.add_argument("--lab", help="lab dir — measured for storage-size metric (memory + index)")
    ap.add_argument("--dataset", choices=["longmemeval", "locomo"], default="longmemeval",
                    help="dataset family — selects the judge-prompt routing for the "
                    "question categories (LoCoMo names map onto upstream templates)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    hyp_path = Path(args.hyp)
    ref_path = Path(args.ref)
    if not hyp_path.exists():
        _fail(f"hypotheses not found: {hyp_path}")
    if not ref_path.exists():
        _fail(f"reference dataset not found: {ref_path}")

    if args.canonical:
        rc = run_canonical_upstream(hyp_path, ref_path, args.judge_short, args.timeout * 100)
        sys.exit(rc)

    hyps = load_hypotheses(hyp_path)
    ref = load_reference(ref_path)
    judge = args.judge
    canonical_label = CANONICAL_JUDGE_ID if _is_gpt4o_class(judge) else (judge or "gateway-default")
    is_canonical = _is_gpt4o_class(judge)

    results_out = Path(args.results_out) if args.results_out else \
        Path(str(hyp_path) + f".eval-results-{(judge or 'gateway').replace('/', '_')}")

    per_cat: dict[str, list[int]] = {c: [] for c in CATEGORIES}
    abstention: list[int] = []
    n_graded = 0
    n_judge_error = 0
    graded_entries = []

    for entry in hyps:
        qid = entry.get("question_id")
        if qid not in ref:
            _log(f"skip {qid}: not in reference")
            continue
        rentry = ref[qid]
        qtype = rentry.get("question_type")
        question = rentry.get("question", "")
        gold = rentry.get("answer", "")
        hypothesis = entry.get("hypothesis", "")
        is_abs = "_abs" in str(qid)
        # LoCoMo uses its own reasoning-type names; the vendored (pinned) upstream
        # judge only knows LongMemEval's 6 tasks. Translate LoCoMo categories to
        # the closest upstream judge template WITHOUT editing the vendored file:
        #   single-hop/multi-hop/open-domain -> 'multi-session' (generic contains-answer),
        #   temporal                          -> 'temporal-reasoning' (off-by-one tolerant),
        #   adversarial                       -> abstention path (unanswerable check).
        judge_task = qtype
        judge_abs = is_abs
        if args.dataset == "locomo":
            if qtype == "adversarial" or rentry.get("is_adversarial"):
                judge_abs = True
                judge_task = "adversarial"
            elif qtype == "temporal":
                judge_task = "temporal-reasoning"
            else:  # single-hop | multi-hop | open-domain | category_N
                judge_task = "multi-session"
        if judge == "stub":
            # OFFLINE-CI sentinel: no gateway call, no spend. Deterministic
            # substring grader (gold tokens present in hypothesis, case-insensitive).
            # NOT a real judge — canonical_judge stays False so no result is ever
            # mistaken for a citable number. Used by longitudinal_run --stub-judge
            # and any offline smoke. A real run passes a frontier-class --judge.
            gl = str(gold).lower().strip()
            hl = str(hypothesis).lower()
            if judge_abs:
                label = any(t in hl for t in ("not mention", "no information",
                            "cannot", "don't know", "do not know", "unanswer"))
            else:
                label = bool(gl) and gl in hl
            raw = f"__STUB__ {label}"
        else:
            # Build the official judge prompt only for a REAL gateway call. The
            # vendored (pinned) upstream get_anscheck_prompt only knows the 6
            # LongMemEval tasks (+ our LoCoMo translation above); an unknown
            # question_type (e.g. Phase-2 'longitudinal-*') raises NotImplementedError.
            # For those we fall back to the generic contains-answer template so the
            # pinned file stays byte-unchanged.
            try:
                prompt = get_anscheck_prompt(judge_task, question, gold, hypothesis,
                                             abstention=judge_abs)
            except NotImplementedError:
                prompt = get_anscheck_prompt("multi-session", question, gold,
                                             hypothesis, abstention=judge_abs)
            label, raw = judge_via_gateway(prompt, judge, args.timeout)
        if raw.startswith("__ERROR__"):
            n_judge_error += 1
        entry_out = dict(entry)
        entry_out["autoeval_label"] = {"model": canonical_label, "label": bool(label)}
        graded_entries.append(entry_out)
        # per-category accounting uses the DATASET's own category (LoCoMo names
        # are added to per_cat on the fly so LoCoMo per-category accuracy renders).
        if qtype not in per_cat:
            per_cat[qtype] = []
        per_cat[qtype].append(1 if label else 0)
        if judge_abs:
            abstention.append(1 if label else 0)
        n_graded += 1
        if n_graded % 10 == 0:
            _log(f"  graded {n_graded}/{len(hyps)}")

    results_out.parent.mkdir(parents=True, exist_ok=True)
    with results_out.open("w", encoding="utf-8") as fh:
        for e in graded_entries:
            fh.write(json.dumps(e) + "\n")

    def _acc(v: list[int]) -> float | None:
        return round(sum(v) / len(v), 4) if v else None

    per_category = {c: {"accuracy": _acc(v), "n": len(v)} for c, v in per_cat.items()}
    all_flat = [x for v in per_cat.values() for x in v]
    task_means = [sum(v) / len(v) for v in per_cat.values() if v]
    metrics = {
        "judge_model": judge or "gateway-default",
        "judge_stamped_as": canonical_label,
        "canonical_judge": is_canonical,
        "n_graded": n_graded,
        "n_judge_error": n_judge_error,
        "overall_accuracy": round(sum(all_flat) / len(all_flat), 4) if all_flat else None,
        "task_averaged_accuracy": round(sum(task_means) / len(task_means), 4) if task_means else None,
        "per_category": per_category,
        "abstention_accuracy": _acc(abstention),
        "abstention_n": len(abstention),
        "results_file": str(results_out),
    }

    # 1b: fold in retrieval recall/precision + cost/latency/storage. Inputs
    # default off <hyp> / <lab> conventions; all optional, degrade gracefully.
    rlog_path = Path(args.retrieval_log) if args.retrieval_log else \
        Path(str(hyp_path) + ".retrieval.jsonl")
    lab_dir = Path(args.lab).resolve() if args.lab else None
    gold_dir = Path(args.gold_dir) if args.gold_dir else (
        lab_dir / "sessions" if lab_dir else None)
    rlog = _load_retrieval_log(rlog_path)
    gold = _load_gold(gold_dir) if gold_dir else {}
    aux = compute_aux_metrics(rlog, gold, lab_dir)
    metrics.update(aux)
    metrics["aux_inputs"] = {
        "retrieval_log": str(rlog_path) if rlog else None,
        "gold_dir": str(gold_dir) if gold else None,
        "lab": str(lab_dir) if lab_dir else None,
        "telemetry_questions": len(rlog),
        "gold_questions": len(gold),
    }

    if args.metrics_out:
        Path(args.metrics_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.metrics_out).write_text(json.dumps(metrics, indent=2), encoding="utf-8")
        _log(f"metrics -> {args.metrics_out}")

    _log(f"overall={metrics['overall_accuracy']} task_avg={metrics['task_averaged_accuracy']} "
         f"n={n_graded} judge={judge or 'default'} canonical={is_canonical} errors={n_judge_error}")
    if args.json:
        print(json.dumps(metrics, indent=2))
    else:
        print(f"Overall Accuracy: {metrics['overall_accuracy']} ({n_graded})")
        print(f"Task-averaged Accuracy: {metrics['task_averaged_accuracy']}")
        for c in CATEGORIES:
            pc = per_category[c]
            print(f"\t{c}: {pc['accuracy']} ({pc['n']})")
        print(f"Abstention: {metrics['abstention_accuracy']} ({len(abstention)})")
        if not is_canonical:
            print("[note] non-canonical judge — number is directional, not leaderboard-canonical")


if __name__ == "__main__":
    main()
