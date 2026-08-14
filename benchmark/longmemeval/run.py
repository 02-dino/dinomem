#!/usr/bin/env python3
"""
run.py — one-command LongMemEval runner for dinomem.

Wires the whole harness end-to-end for ONE arm:
    setup_lab -> adapter(fetch+emit) -> drive_base(converge) -> answer -> score
    -> results/<arm>_latest.md  (+ machine-readable results/<arm>_metrics.json)
then tears the lab down and deletes the ephemeral dataset payload.

ARMS (the single variable is the memory engine):
  --arm base    : dinomem base pipeline only (public floor)
  --arm neuron  : base + neuron overlay (neuron is an UPGRADE LAYER, never
                  standalone). The neuron overlay + neuron recall are applied by
                  an arm hook (see --overlay-cmd / neuron harness); this runner
                  stays engine-agnostic so BOTH arms share ONE protocol.

MODE:
  --sample (default): first N questions (cheap smoke / dev)
  --full            : whole LongMemEval-S (citation-grade); requires --yes

SAFETY / HONESTY GUARDS:
  - COST ESTIMATE printed BEFORE any spend; --full requires --yes to proceed.
  - ISOLATION: everything runs in a throwaway lab; live workspace asserted untouched.
  - EPHEMERAL DATASET: downloaded, hash-verified, used, DELETED. SHA+hash stamped.
  - DETERMINISM GUARD: --determinism runs the answer+score twice and flags drift
    instead of reporting a shaky number.

This runner does NOT reimplement any stage — it shells the harness scripts
(adapter.py, setup_lab.py, drive_base.py, answer.py, score.py) so each stays the
single source of truth.

USAGE:
  python3 run.py --arm base --sample --n 20 \\
      --answer-model <mid-tier> --judge-model <gpt-4o-class>
  python3 run.py --arm base --full --yes --answer-model ... --judge-model ...
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Bump when the answer/hypothesis/scoring PROTOCOL changes in a way that makes
# old runs incomparable (e.g. hypothesis file shape, judge prompt). Part of the
# shared-protocol hash so a stale arm can't be silently compared against a new one.
PROTOCOL_VERSION = "1c-2026-08-14"


def protocol_hash(dataset_info: dict, answer_model: str, judge_model: str,
                  mode: str, n) -> str:
    """A single hash over EVERYTHING that must be identical for a cross-arm
    comparison to be fair EXCEPT the memory engine (the arm). If two arms share
    this hash, they ran the same dataset+SHA, same answer model, same judge, same
    sample — so their metric delta is attributable to the engine alone. The
    3-arm compare_report refuses to compare arms whose protocol_hash differs.
    """
    key = json.dumps({
        "protocol_version": PROTOCOL_VERSION,
        "dataset_family": dataset_info.get("family"),
        "dataset_line": dataset_info.get("line"),
        "dataset_revision": dataset_info.get("revision"),
        "dataset_sha256": dataset_info.get("sha256"),
        "answer_model": answer_model or "gateway-default",
        "judge_model": judge_model or "gateway-default",
        "mode": mode,
        "n": n,
    }, sort_keys=True)
    return hashlib.sha256(key.encode()).hexdigest()

HERE = Path(__file__).parent.resolve()
RESULTS = HERE / "results"
LEADERBOARD = "https://github.com/xiaowu0162/LongMemEval"

# Rough per-question token estimates (measured-from-smoke should refine these;
# used ONLY for the pre-run cost ESTIMATE, never for scoring).
EST_ANSWER_TOKENS = 1800   # retrieved context + question + answer, per Q
EST_JUDGE_TOKENS = 400     # judge prompt + 10-token verdict, per Q

# Indicative $/1K-token (input+output blended) for the cost-estimate ONLY. These
# are ballparks for common tiers; the real bill depends on the user's provider.
PRICE_HINT = {
    "gpt-4o": 0.0075, "gpt-4o-2024-08-06": 0.0075,
    "gpt-4o-mini": 0.0004, "gpt-4o-mini-2024-07-18": 0.0004,
    "default": 0.002,
}


def _fail(msg: str, code: int = 1):
    print(f"[run] FAIL: {msg}", file=sys.stderr)
    sys.exit(code)


def _log(msg: str):
    print(f"[run] {msg}", file=sys.stderr)


def _sh(cmd: list[str], timeout: int, capture=True,
        env: dict | None = None) -> subprocess.CompletedProcess:
    _log("$ " + " ".join(str(c) for c in cmd))
    return subprocess.run([str(c) for c in cmd], timeout=timeout,
                          capture_output=capture, text=True, env=env)


def _price(model: str) -> float:
    m = (model or "").lower()
    for k, v in PRICE_HINT.items():
        if k in m:
            return v
    return PRICE_HINT["default"]


def cost_estimate(n: int, answer_model: str, judge_model: str) -> dict:
    # TOKEN-FIRST: on subscription plans the $ is meaningless (flat fee), so the
    # real, provider-agnostic currency is TOKENS. Tokens are the headline; the $
    # is a best-effort footnote only (accurate only on metered/pay-go providers).
    ans_tokens = n * EST_ANSWER_TOKENS
    judge_tokens = n * EST_JUDGE_TOKENS
    total_tokens = ans_tokens + judge_tokens
    ans_cost = ans_tokens / 1000.0 * _price(answer_model)
    judge_cost = judge_tokens / 1000.0 * _price(judge_model)
    total = ans_cost + judge_cost
    return {
        "n_questions": n,
        "answer_model": answer_model or "gateway-default",
        "judge_model": judge_model or "gateway-default",
        # --- token-first headline (provider-agnostic; the real cost on subs) ---
        "est_answer_tokens": ans_tokens,
        "est_judge_tokens": judge_tokens,
        "est_total_tokens": total_tokens,
        # --- $ footnote (metered providers only; ignore on flat-fee subs) ---
        "est_answer_usd": round(ans_cost, 3),
        "est_judge_usd": round(judge_cost, 3),
        "est_total_usd_low": round(total * 0.6, 2),
        "est_total_usd_high": round(total * 1.8, 2),
        "note": "TOKEN-first estimate (est_total_tokens = the real cost on "
                "subscription plans). $ figures are a ROUGH footnote from "
                "indicative metered prices; ignore on flat-fee subs. Prints "
                "before any spend.",
    }


def print_cost(est: dict):
    print("\n=== COST ESTIMATE (before any spend) ===", file=sys.stderr)
    print(f"  questions:      {est['n_questions']}", file=sys.stderr)
    print(f"  answer model:   {est['answer_model']}", file=sys.stderr)
    print(f"  judge model:    {est['judge_model']}", file=sys.stderr)
    # TOKENS = the headline (real cost on subscription plans).
    print(f"  est. answer tok:{est['est_answer_tokens']:>10,}", file=sys.stderr)
    print(f"  est. judge  tok:{est['est_judge_tokens']:>10,}", file=sys.stderr)
    print(f"  EST. TOTAL TOK: {est['est_total_tokens']:>10,}", file=sys.stderr)
    # $ = footnote only (metered providers; ignore on flat-fee subs).
    print(f"  ($ footnote:    ${est['est_total_usd_low']}–${est['est_total_usd_high']} "
          f"metered-only, ignore on subs)", file=sys.stderr)
    print("========================================\n", file=sys.stderr)


# ---------------------------------------------------------------------------
def _run_pipeline_once(arm, lab_info, dataset_path, sample_index, n, qids_file,
                       answer_model, judge_model, recall, overlay_cmd,
                       out_prefix, timeout, dataset_family="longmemeval",
                       ref_path=None) -> dict:
    """One full answer+score pass over the (already-converged) lab. Returns metrics.

    dataset_family: 'longmemeval' | 'locomo' — selects score.py judge routing.
    ref_path: the reference the scorer grades against. For LongMemEval this IS the
      dataset file. For LoCoMo the dataset is a list-of-conversations, so the
      caller passes an answer.py-shaped per-question ref file (built by
      adapter_locomo questions) instead.
    """
    lab = Path(lab_info["lab"])
    hyp = RESULTS / f"{out_prefix}_hypotheses.jsonl"
    metrics_out = RESULTS / f"{out_prefix}_metrics.json"
    ref = ref_path or dataset_path

    # ANSWER
    ans_cmd = [sys.executable, HERE / "answer.py",
               "--lab", lab, "--dataset", ref, "--out", hyp,
               "--recall", recall, "--json"]
    if qids_file:
        ans_cmd += ["--qids-file", qids_file]
    elif n is not None:
        ans_cmd += ["--n", n]
    if answer_model:
        ans_cmd += ["--model", answer_model]
    ar = _sh(ans_cmd, timeout)
    if ar.returncode != 0:
        _fail(f"answer.py failed: {ar.stderr[-400:]}")

    # SCORE
    score_cmd = [sys.executable, HERE / "score.py",
                 "--hyp", hyp, "--ref", ref,
                 "--lab", str(lab), "--dataset", dataset_family,
                 "--metrics-out", metrics_out, "--json"]
    if judge_model:
        score_cmd += ["--judge", judge_model]
    sr = _sh(score_cmd, timeout)
    if sr.returncode != 0:
        _fail(f"score.py failed: {sr.stderr[-400:]}")
    try:
        return json.loads(Path(metrics_out).read_text())
    except Exception as e:  # noqa: BLE001
        _fail(f"cannot read metrics {metrics_out}: {e}")


def _stamp_md(arm, mode, n, metrics, dataset_info, answer_model, judge_model,
              determinism, seconds) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    pc = metrics.get("per_category", {})
    lines = [
        f"# LongMemEval-S — dinomem {arm} arm",
        "",
        f"- **Arm:** {arm}" + {
            "neuron": "  (base + neuron overlay)",
            "rag": "  (plain vector RAG floor — no dinomem pipeline)",
            "base": "  (base only)",
        }.get(arm, ""),
        f"- **Mode:** {mode}   **N:** {n}",
        f"- **Overall accuracy:** {metrics.get('overall_accuracy')}",
        f"- **Task-averaged accuracy:** {metrics.get('task_averaged_accuracy')}",
        f"- **Abstention accuracy:** {metrics.get('abstention_accuracy')} "
        f"({metrics.get('abstention_n')})",
        "",
        "## Per-category accuracy",
        "",
        "| Category | Accuracy | N |",
        "|-|-|-|",
    ]
    for cat, d in pc.items():
        lines.append(f"| {cat} | {d.get('accuracy')} | {d.get('n')} |")
    lines += [
        "",
        "## Run stamp (disclosed, for reproducibility)",
        "",
        f"- **Answer model:** {answer_model or 'gateway-default'}",
        f"- **Judge model:** {judge_model or 'gateway-default'} "
        f"(canonical={metrics.get('canonical_judge')}, stamped_as={metrics.get('judge_stamped_as')})",
        f"- **Dataset line:** {dataset_info.get('line')}",
        f"- **Dataset file:** {dataset_info.get('file')}",
        f"- **HF revision SHA:** {dataset_info.get('revision')}",
        f"- **Dataset sha256:** {dataset_info.get('sha256')}",
        f"- **Dataset hash verified:** {dataset_info.get('hash_verified')}",
        f"- **Judge errors:** {metrics.get('n_judge_error')}",
        f"- **Determinism:** {determinism.get('verdict')} "
        f"(overall run1={determinism.get('overall_1')} run2={determinism.get('overall_2')})"
        if determinism else "- **Determinism:** not run (single pass)",
        f"- **Wall time:** {seconds}s",
        f"- **Generated:** {now}",
        f"- **Official leaderboard / paper:** {LEADERBOARD}",
        "",
        "> Number is comparable to published LongMemEval-S results ONLY when dataset "
        "line + a frontier-class judge + hypothesis format match the convention "
        "(gpt-4o was the reference judge as of 2024; use whatever current frontier "
        "model matches the convention when you run). "
        "A non-canonical judge makes this directional, not leaderboard-canonical.",
        "",
    ]
    if not metrics.get("canonical_judge"):
        lines.insert(3, "> ⚠️ **Non-canonical judge** — directional number, not "
                        "leaderboard-canonical. Use a current frontier-class judge "
                        "(gpt-4o-class or better) for a citable figure.")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="dinomem LongMemEval runner (one arm, end-to-end).")
    ap.add_argument("--arm", choices=["rag", "base", "neuron"], default="base")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--sample", action="store_true", help="first N questions (default)")
    mode.add_argument("--full", action="store_true", help="whole LongMemEval-S (needs --yes)")
    ap.add_argument("--n", type=int, default=20, help="sample size (sample mode)")
    ap.add_argument("--yes", action="store_true", help="confirm a --full (paid) run")
    ap.add_argument("--dataset", choices=["longmemeval", "locomo"], default="longmemeval",
                    help="benchmark family. longmemeval=1 haystack/question; "
                    "locomo=1 conversation/many questions (uses adapter_locomo).")
    ap.add_argument("--dataset-line", choices=["original", "cleaned"], default="cleaned",
                    help="which official LongMemEval dataset line (cleaned=current standard)")
    ap.add_argument("--answer-model", default=os.environ.get("DINOMEM_BENCH_ANSWER_MODEL", ""))
    ap.add_argument("--judge-model", default=os.environ.get("DINOMEM_BENCH_JUDGE_MODEL", ""))
    ap.add_argument("--recall", choices=["base", "command"], default=None,
                    help="override recall (default: base for base arm, command for neuron)")
    ap.add_argument("--overlay-cmd", default=os.environ.get("DINOMEM_BENCH_OVERLAY_CMD", ""),
                    help="neuron arm: command that applies the neuron overlay onto the lab "
                    "WS. {ws}/{lab} are substituted. The canonical form (from the neuron "
                    "installer) is:  bash <neuron-repo>/scripts/install.sh --workspace {ws} "
                    "--agent-id <id> --agree --no-cron --no-auto-base  — note --no-auto-base "
                    "is REQUIRED for arm fairness: base is already laid down, so the overlay "
                    "must NOT re-refresh base from GitHub mid-run (breaks isolation + "
                    "protocol-hash reproducibility). See .env.example.")
    ap.add_argument("--sample-index", type=int, default=0,
                    help="which dataset instance's haystack becomes the lab memory")
    ap.add_argument("--determinism", action="store_true",
                    help="run answer+score twice; flag drift instead of reporting a shaky number")
    ap.add_argument("--determinism-tol", type=float, default=0.0,
                    help="allowed |overall1-overall2| before flagging (default exact match)")
    ap.add_argument("--keep-lab", action="store_true", help="do not teardown the lab (debug)")
    ap.add_argument("--source", default=os.environ.get("DINOMEM_WORKSPACE", ""),
                    help="installed dinomem workspace (source of procedures/tools)")
    ap.add_argument("--agent-id", default=os.environ.get("DINOMEM_BENCH_AGENT_ID", "analyst"),
                    help="agent id for the lab workspace layout + neuron overlay (default: analyst)")
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--estimate-only", action="store_true",
                    help="print the cost estimate and EXIT (no lab, no spend).")
    args = ap.parse_args()

    mode_name = "full" if args.full else "sample"
    if args.full and not args.yes:
        _fail("--full is a paid citation-grade run; re-run with --yes to confirm "
              "(a cost estimate is printed first).")
    if not args.source:
        _fail("--source (or DINOMEM_WORKSPACE) required: the installed dinomem workspace")

    # rag + neuron both use the generic external-recall command hook; base uses
    # its lexical-over-distilled-memory path.
    recall = args.recall or ("base" if args.arm == "base" else "command")
    RESULTS.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    # ---- 0. cost estimate BEFORE any spend ----
    # N is exact for sample; for full we don't know until dataset loads, so estimate 500.
    est_n = args.n if mode_name == "sample" else 500
    est = cost_estimate(est_n, args.answer_model, args.judge_model)
    print_cost(est)
    (RESULTS / f"{args.arm}_cost_estimate.json").write_text(json.dumps(est, indent=2))
    if args.estimate_only:
        _log("--estimate-only: no lab built, no model calls made. Exiting.")
        print(json.dumps({"estimate_only": True, "arm": args.arm, "mode": mode_name,
                          "n": est_n, "estimate": est}, indent=2))
        sys.exit(0)

    # ---- 1. lab bootstrap ----
    # Base arm: FLAT layout (self-contained, SESSIONS_DIR patched).
    # Neuron arm: REAL openclaw-tree layout so the neuron installer's derived
    #   SESSIONS_DIR lands inside the sandbox (see setup_lab.py --layout real).
    setup_cmd = [sys.executable, HERE / "setup_lab.py", "--source", args.source, "--json"]
    if args.arm == "neuron":
        setup_cmd += ["--layout", "real", "--agent-id", args.agent_id]
    setup = _sh(setup_cmd, args.timeout)
    if setup.returncode != 0:
        _fail(f"setup_lab.py failed: {setup.stderr[-400:]}")
    lab_info = json.loads(setup.stdout[setup.stdout.find("{"):])
    lab = lab_info["lab"]          # sandbox root (carries .dinomem_lab sentinel)
    # WS = where the pipeline actually runs. flat: lab itself; real: workspace-<agent>.
    ws = lab_info.get("workspace", lab)
    _log(f"lab = {lab}  ws = {ws}  layout = {lab_info.get('layout','flat')}")

    dataset_tmp = None
    # LoCoMo: one conversation carries MANY questions, so answer.py grades against
    # an answer.py-shaped per-question REF file (built by adapter_locomo questions),
    # NOT the raw dataset. LongMemEval grades against the dataset directly.
    ref_path = None
    adapter_mod = "adapter_locomo.py" if args.dataset == "locomo" else "adapter.py"
    try:
        # ---- 2. adapter: fetch (ephemeral) + emit sample haystack -> lab sessions ----
        fetch_out = Path(lab_info["lab"]) / ".dataset_tmp"
        fetch_cmd = [sys.executable, HERE / adapter_mod, "fetch",
                     "--out", fetch_out, "--json"]
        # dataset-line knob is LongMemEval-only; LoCoMo has a single file.
        if args.dataset == "longmemeval":
            fr = _sh(fetch_cmd + ["--dataset-line", args.dataset_line], args.timeout)
            if fr.returncode != 0:
                fr = _sh(fetch_cmd, args.timeout)  # retry w/o flag for older builds
        else:
            fr = _sh(fetch_cmd, args.timeout)
        if fr.returncode != 0:
            _fail(f"{adapter_mod} fetch failed: {fr.stderr[-400:]}")
        fetch_info = json.loads(fr.stdout[fr.stdout.find("{"):])
        dataset_path = fetch_info.get("path") or fetch_info.get("file")
        dataset_tmp = dataset_path
        dataset_info = {
            "family": args.dataset,
            "line": args.dataset_line if args.dataset == "longmemeval" else "locomo10",
            "file": Path(dataset_path).name if dataset_path else "?",
            "revision": fetch_info.get("revision"),
            "sha256": fetch_info.get("sha256"),
            "hash_verified": fetch_info.get("hash_verified"),
        }

        # emit the chosen sample's haystack into the lab sessions dir.
        # flat: <lab>/sessions ; real: <lab>/agents/<agent>/sessions. adapter reads
        # the sessions_dir from the lab_info the runner passes through --sessions-dir
        # when present; else falls back to --lab convention.
        emit_cmd = [sys.executable, HERE / adapter_mod, "emit",
                    "--dataset", dataset_path, "--index", args.sample_index,
                    "--lab", lab, "--json"]
        if args.dataset == "longmemeval" and lab_info.get("sessions_dir"):
            emit_cmd += ["--sessions-dir", lab_info["sessions_dir"]]
        er = _sh(emit_cmd, args.timeout)
        if er.returncode != 0:
            # retry without --sessions-dir for older adapter builds (flat convention)
            er = _sh([c for c in emit_cmd if c not in ("--sessions-dir", lab_info.get("sessions_dir"))],
                     args.timeout)
            if er.returncode != 0:
                _fail(f"{adapter_mod} emit failed: {er.stderr[-400:]}")

        # LoCoMo: build the per-question REF file for this conversation so answer.py
        # + score.py operate per-question (the dataset itself is 1 conversation).
        if args.dataset == "locomo":
            ref_path = str(RESULTS / f"{args.arm}_locomo_ref.json")
            q_cmd = [sys.executable, HERE / "adapter_locomo.py", "questions",
                     "--dataset", dataset_path, "--index", args.sample_index,
                     "--out", ref_path]
            qr = _sh(q_cmd, args.timeout)
            if qr.returncode != 0:
                _fail(f"adapter_locomo questions failed: {qr.stderr[-400:]}")
            _log(f"locomo per-question ref -> {ref_path}")

        # ---- 2a. RAG arm: build the embedding index over the raw emitted haystack ----
        # The rag arm has NO dinomem pipeline and NO distilled memory to converge;
        # its corpus IS the raw haystack archive. Build the vector index now, and
        # point the answer loop's external-recall hook at rag_recall.py query.
        if args.arm == "rag":
            idx_cmd = [sys.executable, HERE / "rag_recall.py", "index", "--lab", lab]
            ir = _sh(idx_cmd, args.timeout)
            if ir.returncode != 0:
                _fail(f"rag_recall index failed: {ir.stderr[-400:]}")
            _log("rag index built (raw haystack chunks embedded)")
            # auto-wire the recall command the answer loop shells (unless user set one)
            if not os.environ.get("DINOMEM_BENCH_RECALL_CMD", "").strip():
                os.environ["DINOMEM_BENCH_RECALL_CMD"] = (
                    f'{sys.executable} {HERE / "rag_recall.py"} query '
                    f'--lab {lab} "{{q}}" --k {{k}} --json'
                )

        # ---- 2b. neuron overlay (arm B only): run the neuron installer onto the WS ----
        if args.arm == "neuron":
            if not args.overlay_cmd:
                _fail("--arm neuron requires --overlay-cmd (or DINOMEM_BENCH_OVERLAY_CMD): "
                      "the command that installs the neuron overlay onto the WS. "
                      "Neuron is base+overlay, never standalone. Typical:\n"
                      "  bash <neuron-repo>/scripts/install.sh --workspace {ws} "
                      "--agent-id " + args.agent_id + " --agree --no-cron --no-auto-base")
            # {ws} = the workspace-<agent> dir (correct --workspace for the installer);
            # {lab} = the sandbox root. Support both tokens.
            ov_cmd = args.overlay_cmd.replace("{ws}", ws).replace("{lab}", lab)
            # CONFIG ISOLATION (critical): the neuron installer patches an
            # openclaw.json (plugins.load.paths / plugins.entries.*.config.
            # workspaceDir) and defaults to ${OPENCLAW_CONFIG:-$HOME/.openclaw/
            # openclaw.json} -- i.e. the caller's REAL config. Left unsandboxed it
            # writes LAB-TEMP paths into the real config; when the lab is torn down
            # those dangling paths crash-loop the user's gateway (exit 78). Point
            # the installer at a LAB-LOCAL config/home so every config write stays
            # inside the sandbox and vanishes with it.
            lab_ocdir = os.path.join(lab, ".openclaw")
            os.makedirs(lab_ocdir, exist_ok=True)
            ov_env = dict(os.environ)
            ov_env["HOME"] = lab
            ov_env["OPENCLAW_DIR"] = lab_ocdir
            ov_env["OPENCLAW_CONFIG"] = os.path.join(lab_ocdir, "openclaw.json")
            ov = _sh(["bash", "-c", ov_cmd], args.timeout, env=ov_env)
            if ov.returncode != 0:
                _fail(f"neuron overlay failed: {ov.stderr[-400:]}")

        # ---- 3. drive pipeline to convergence (isolation tripwire) ----
        # RAG arm: NO pipeline to drive (naive floor = retrieval over raw sessions).
        # We still assert the live source is untouched (isolation) before proceeding.
        # Base arm -> drive_base.py --lab <lab>. Neuron arm -> drive_neuron.py --ws
        # <ws> --sandbox-root <lab> (forces base chain + neuron L2/L3/L4, asserts
        # base items>0 AND graph nodes>0).
        if args.arm == "rag":
            live_now = os.path.getmtime(args.source) if os.path.exists(args.source) else None
            live_then = lab_info.get("live_source_mtime")
            if live_then is not None and live_now is not None and str(live_now) != str(live_then):
                _fail(f"ISOLATION BREACH: live source mtime changed "
                      f"({live_then} -> {live_now}) during rag setup")
            _log("rag arm: no pipeline drive (retrieval over raw haystack); isolation OK")
            drive_res = {"ok": True, "arm": "rag", "note": "no-pipeline (naive RAG floor)"}
        elif args.arm == "neuron":
            drive_cmd = [sys.executable, HERE / "drive_neuron.py", "--ws", ws,
                         "--sandbox-root", lab,
                         "--live-source", args.source,
                         "--live-source-mtime", lab_info["live_source_mtime"], "--json"]
            drive_label = "drive_neuron"
        else:
            drive_cmd = [sys.executable, HERE / "drive_base.py", "--lab", lab,
                         "--live-source", args.source,
                         "--live-source-mtime", lab_info["live_source_mtime"], "--json"]
            drive_label = "drive_base"
        dr = _sh(drive_cmd, args.timeout)
        drive_res = json.loads(dr.stdout[dr.stdout.find("{"):]) if dr.stdout.strip() else {}
        if dr.returncode != 0 or not drive_res.get("ok"):
            _fail(f"{drive_label} did not converge / isolation issue: "
                  f"{drive_res.get('reason')} :: {dr.stderr[-300:]}")

        # ---- 4. answer + score (once, or twice for determinism) ----
        n_arg = args.n if mode_name == "sample" else None
        m1 = _run_pipeline_once(args.arm, lab_info, dataset_path, args.sample_index,
                                n_arg, None, args.answer_model, args.judge_model,
                                recall, args.overlay_cmd, f"{args.arm}", args.timeout,
                                dataset_family=args.dataset, ref_path=ref_path)
        determinism = None
        if args.determinism:
            m2 = _run_pipeline_once(args.arm, lab_info, dataset_path, args.sample_index,
                                    n_arg, None, args.answer_model, args.judge_model,
                                    recall, args.overlay_cmd, f"{args.arm}_run2", args.timeout,
                                    dataset_family=args.dataset, ref_path=ref_path)
            o1, o2 = m1.get("overall_accuracy"), m2.get("overall_accuracy")
            drift = abs((o1 or 0) - (o2 or 0))
            determinism = {
                "overall_1": o1, "overall_2": o2, "drift": round(drift, 4),
                "tolerance": args.determinism_tol,
                "verdict": "STABLE" if drift <= args.determinism_tol else "NONDETERMINISTIC",
            }
            if determinism["verdict"] == "NONDETERMINISTIC":
                _log(f"⚠️ determinism FAILED: run1={o1} run2={o2} drift={drift} "
                     f"> tol={args.determinism_tol}. Reporting the flag, not a shaky number.")

        # ---- 5. write results/<arm>_latest.md ----
        n_final = m1.get("n_graded", args.n)
        seconds = round(time.time() - t0, 1)
        md = _stamp_md(args.arm, mode_name, n_final, m1, dataset_info,
                       args.answer_model, args.judge_model, determinism, seconds)
        out_md = RESULTS / f"{args.arm}_latest.md"
        out_md.write_text(md, encoding="utf-8")
        # also drop a machine-readable arm result the comparison generator reads
        arm_result = {
            "arm": args.arm, "mode": mode_name, "n": n_final,
            "metrics": m1, "dataset": dataset_info,
            "answer_model": args.answer_model or "gateway-default",
            "judge_model": args.judge_model or "gateway-default",
            "determinism": determinism, "seconds": seconds,
            "protocol_version": PROTOCOL_VERSION,
            "protocol_hash": protocol_hash(
                dataset_info, args.answer_model, args.judge_model,
                mode_name, n_final),
            "generated": datetime.now(timezone.utc).isoformat(),
        }
        (RESULTS / f"{args.arm}_result.json").write_text(json.dumps(arm_result, indent=2))
        _log(f"wrote {out_md} and {args.arm}_result.json")
        print(md)

    finally:
        # ---- ephemeral dataset delete + lab teardown ----
        if dataset_tmp and Path(dataset_tmp).exists():
            try:
                Path(dataset_tmp).unlink()
                _log(f"deleted ephemeral dataset payload {dataset_tmp}")
            except OSError as e:
                _log(f"WARN could not delete dataset payload: {e}")
        if not args.keep_lab:
            td = _sh([sys.executable, HERE / "setup_lab.py", "--teardown", lab], 120)
            _log("lab torn down" if td.returncode == 0 else f"WARN teardown rc={td.returncode}")
        else:
            _log(f"--keep-lab: left lab at {lab}")


if __name__ == "__main__":
    main()
