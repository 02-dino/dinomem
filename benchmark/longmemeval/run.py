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

# Per-question token estimates. THREE cost buckets, because the pipeline's real
# cost is NOT the answer step — it's INGESTING the haystack into memory first.
# A naive answer+judge-only estimate under-reports by ~50x. Measured-from-smoke
# should refine these; used ONLY for the pre-run ESTIMATE, never for scoring.
#
#   INGEST: the pipeline (base/neuron) reads the whole haystack into memory
#           (extract -> consolidate -> review) ONCE per question. This is the
#           dominant term. LongMemEval-S haystack ~= 115k tokens/Q (official
#           'S' size, ~500 sessions); LoCoMo ~= 25k tokens/Q (10 long convs).
#   ANSWER: retrieved context + question + generated answer, per Q (small).
#   JUDGE : judge prompt + short verdict, per Q (smallest).
#
# Ingestion runs on arms WITH a real memory pipeline (base, neuron). The rag arm
# chunks+embeds LOCALLY (TEI, ~free) so its LLM ingest ~= 0. All arms pay ANSWER+
# JUDGE. So per-arm cost is NOT uniform — the old estimate wrongly assumed it was.
EST_ANSWER_TOKENS = 1800     # retrieved context + question + answer, per Q
EST_JUDGE_TOKENS = 400       # judge prompt + short verdict, per Q
# per-dataset RAW haystack size tokens/Q. This is the size of the content ONCE.
# It is NOT the ingest cost by itself — see EST_INGEST_TOKENS below, which applies
# the windowing multiplier. (GUESS until a --sample smoke measures true per-Q.)
RAW_HAYSTACK_TOKENS = {
    "longmemeval": 115000,   # official LongMemEval-S ~115k-token haystack/Q
    "locomo": 25000,         # LoCoMo ~10 long multi-session convs/Q
}
# WINDOWING OVERHEAD (why the ingest estimate is NOT just the haystack size):
# extract_memory.py splits any oversized archive into <=WINDOW_MAX_CHARS(~80k-char)
# passes and runs ONE LLM call per window. Each call re-sends its window PLUS the
# fixed extraction prompt scaffold (schema + rules, ~2k tokens). So a 115k-token
# haystack is NOT read once — it is read across ~ceil(size/window) calls, and the
# scaffold is paid per call. Real ingest tokens therefore EXCEED the raw haystack
# size. We model this so a full-run user is not shocked by a bill above the
# naive "haystack size x N questions" figure. A --sample smoke refines it.
#   ~80k chars/window ~= ~20k tokens/window (4 chars/token). 115k-tok haystack
#   (~460k chars) -> ~6 windows; scaffold ~2k tok/window -> ~12k scaffold overhead
#   plus modest chunk-boundary re-send. Net ~1.15x on the big LongMemEval haystack,
#   higher relative overhead on smaller ones. Conservative single multiplier:
WINDOW_INGEST_MULTIPLIER = 1.2   # ingest_tokens = raw_haystack * this (windowing overhead)
# per-dataset EFFECTIVE ingest tokens/Q = raw haystack x windowing overhead.
EST_INGEST_TOKENS = {
    ds: int(raw * WINDOW_INGEST_MULTIPLIER)
    for ds, raw in RAW_HAYSTACK_TOKENS.items()
}
# which arms run the LLM memory pipeline (pay INGEST). rag ingests locally (~free).
ARMS_WITH_INGEST = {"base", "neuron"}

# Full-run question counts per dataset (for --estimate-all: what a START-TO-FINISH
# run costs). LongMemEval-S = 500 (official). LoCoMo = 10 conversations, ~199 QA
# each (conv0=199 verified in adapter_locomo.py) => ~1986 questions total.
# These are GUESSES until a smoke run measures true per-Q tokens (LoCoMo haystacks
# are long multi-session -> real ingest tokens could be HIGHER). Treat as a FLOOR.
FULL_Q_COUNTS = {
    "longmemeval": 500,
    "locomo": 1986,
}

# Model-AGNOSTIC price table (indicative $/1M tokens, blended input+output) so a
# user maps TOKENS -> $ for THEIR provider. Tokens are the invariant; $ is just
# tokens x whoever you pick. These are ballpark tiers, NOT a recommendation.
PRICE_TIERS_PER_MTOK = {
    "budget (e.g. flash/mini/haiku tier)": 0.30,
    "mid (e.g. sonnet/gpt-4o tier)": 4.00,
    "frontier (e.g. opus/gpt-4-class tier)": 20.00,
}
ALL_ARMS = ("rag", "base", "neuron")

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


def cost_estimate(n: int, answer_model: str, judge_model: str,
                  arm: str = "base", dataset: str = "longmemeval") -> dict:
    # TOKEN-FIRST: on subscription plans the $ is meaningless (flat fee), so the
    # real, provider-agnostic currency is TOKENS. Tokens are the headline; the $
    # is a best-effort footnote only (accurate only on metered/pay-go providers).
    #
    # INGEST is the dominant term (~93% on LongMemEval) and MUST be included, or a
    # --full user is shocked: they'd see ~1M tokens and be billed ~118M. Ingest is
    # paid only by arms with a real memory pipeline (base/neuron); rag ingests
    # locally (~free). Ingest tokens already fold in the windowing overhead (see
    # EST_INGEST_TOKENS / WINDOW_INGEST_MULTIPLIER).
    ingest_per_q = EST_INGEST_TOKENS.get(dataset, 0) if arm in ARMS_WITH_INGEST else 0
    ingest_tokens = n * ingest_per_q
    ans_tokens = n * EST_ANSWER_TOKENS
    judge_tokens = n * EST_JUDGE_TOKENS
    total_tokens = ingest_tokens + ans_tokens + judge_tokens
    # ingest is generated by the pipeline's extract LLM; price it at the answerer's
    # tier as a proxy (user's real extract model may be cheaper -> this is an
    # UPPER-ish bound on the $ footnote, which is the safe direction for a warning).
    ingest_cost = ingest_tokens / 1000.0 * _price(answer_model)
    ans_cost = ans_tokens / 1000.0 * _price(answer_model)
    judge_cost = judge_tokens / 1000.0 * _price(judge_model)
    total = ingest_cost + ans_cost + judge_cost
    return {
        "n_questions": n,
        "arm": arm,
        "dataset": dataset,
        "answer_model": answer_model or "gateway-default",
        "judge_model": judge_model or "gateway-default",
        # --- token-first headline (provider-agnostic; the real cost on subs) ---
        "est_ingest_tokens": ingest_tokens,
        "est_ingest_tokens_per_q": ingest_per_q,
        "est_answer_tokens": ans_tokens,
        "est_judge_tokens": judge_tokens,
        "est_total_tokens": total_tokens,
        "window_ingest_multiplier": WINDOW_INGEST_MULTIPLIER,
        # --- $ footnote (metered providers only; ignore on flat-fee subs) ---
        "est_ingest_usd": round(ingest_cost, 3),
        "est_answer_usd": round(ans_cost, 3),
        "est_judge_usd": round(judge_cost, 3),
        "est_total_usd_low": round(total * 0.6, 2),
        "est_total_usd_high": round(total * 1.8, 2),
        "note": "TOKEN-first estimate (est_total_tokens = the real cost on "
                "subscription plans). INGEST dominates (~93% on LongMemEval) and is "
                "INCLUDED here for base/neuron (rag ingests locally ~free); ingest "
                f"tokens include the windowing overhead (x{WINDOW_INGEST_MULTIPLIER}). "
                "$ figures are a ROUGH footnote from indicative metered prices; "
                "ignore on flat-fee subs. Per-Q ingest is a GUESS until a --sample "
                "smoke measures it. Prints before any spend.",
    }


# RECOMMENDED model pair for the worked-$ line. The user is FREE to pick any
# models (--answer-model / --judge-model / env); this is just our suggestion,
# shown as a concrete worked example so the $ isn't abstract. Tokens remain the
# invariant headline — the price table below covers whatever you actually choose.
#
# RECOMMENDATION = tier + CROSS-VENDOR, not fixed models:
#   - both answerer AND judge at a frontier/mid tier (gpt-4o, sonnet-4.6, or equiv)
#   - answerer and judge from DIFFERENT vendors (anthropic vs openai vs ...).
#     Never same-vendor both sides (e.g. anthropic answerer + anthropic judge) —
#     a same-family judge can score its own family's style favorably (self-bias).
#   So sonnet-4.6 answerer + gpt-4o judge OR gpt-4o answerer + sonnet-4.6 judge —
#   just not same vendor on both.
RECOMMENDED_ANSWER_MODEL = "sonnet-4.6"
RECOMMENDED_ANSWER_PRICE_PER_MTOK = 4.00    # mid tier
RECOMMENDED_JUDGE_MODEL = "gpt-4o"
RECOMMENDED_JUDGE_PRICE_PER_MTOK = 7.50     # gpt-4o blended

# Vendor inference for the cross-vendor guard (best-effort substring match).
_VENDOR_HINTS = {
    "anthropic": ("claude", "sonnet", "opus", "haiku", "anthropic"),
    "openai": ("gpt", "o1", "o3", "o4", "openai", "chatgpt"),
    "google": ("gemini", "palm", "google"),
    "xai": ("grok", "xai"),
    "meta": ("llama", "meta"),
    "mistral": ("mistral", "mixtral"),
    "deepseek": ("deepseek",),
}


def _infer_vendor(model: str) -> str | None:
    """Best-effort vendor from a model id/alias. None if unknown."""
    if not model:
        return None
    m = model.lower()
    for vendor, hints in _VENDOR_HINTS.items():
        if any(h in m for h in hints):
            return vendor
    return None


def cross_vendor_warning(answer_model: str, judge_model: str) -> str | None:
    """WARN (never block — user's free choice) if answerer + judge look same-vendor,
    which risks same-family scoring bias. Returns a warning string or None."""
    av, jv = _infer_vendor(answer_model), _infer_vendor(judge_model)
    if av and jv and av == jv:
        return (f"answerer and judge are BOTH '{av}' vendor — a same-family judge can "
                f"favor its own family's style (self-bias). RECOMMENDED: cross-vendor "
                f"(e.g. answerer {av} + judge from a different vendor). Proceeding anyway.")
    return None


def estimate_all(answer_model: str, judge_model: str) -> dict:
    """Full START-TO-FINISH estimate: every dataset x every arm, in one call.
    So a user who wants to 'run all' knows the real token/$ budget UP FRONT.
    Pure arithmetic; no lab, no model calls.

    3 BUCKETS (the fix): INGEST dominates and only base/neuron pay it; the old
    estimate counted only answer+judge and under-reported ~50x.
      per-arm tokens = questions * (answer + judge) + [ingest if arm in base/neuron]
    """
    rows = []
    tot_ingest = tot_answer = tot_judge = 0
    for ds, nq in FULL_Q_COUNTS.items():
        ingest_per_q = EST_INGEST_TOKENS.get(ds, 0)
        # ingest paid by base+neuron; answer+judge paid by ALL arms
        n_ingest_arms = len(ARMS_WITH_INGEST & set(ALL_ARMS))
        ds_ingest = nq * ingest_per_q * n_ingest_arms
        ds_answer = nq * EST_ANSWER_TOKENS * len(ALL_ARMS)
        ds_judge = nq * EST_JUDGE_TOKENS * len(ALL_ARMS)
        ds_total = ds_ingest + ds_answer + ds_judge
        tot_ingest += ds_ingest; tot_answer += ds_answer; tot_judge += ds_judge
        rows.append({
            "dataset": ds, "questions": nq,
            "ingest_tokens_per_q": ingest_per_q,
            "ingest_tokens": ds_ingest,   # base+neuron only
            "answer_tokens": ds_answer,
            "judge_tokens": ds_judge,
            "all_arms_tokens": ds_total,
        })
    grand_tok = tot_ingest + tot_answer + tot_judge
    # worked $ using the RECOMMENDED pair (sonnet-4.6 answerer + gpt-4o judge).
    # ingest priced at the answerer's tier (it's the pipeline's generative LLM).
    # User-selectable — this is a suggestion, not a lock.
    a = RECOMMENDED_ANSWER_PRICE_PER_MTOK / 1e6
    j = RECOMMENDED_JUDGE_PRICE_PER_MTOK / 1e6
    recommended_usd = (tot_ingest + tot_answer) * a + tot_judge * j
    # model-agnostic table: whole-run $ at each price tier (blended, all buckets)
    tier_usd = {name: round(grand_tok * (p / 1e6), 2)
                for name, p in PRICE_TIERS_PER_MTOK.items()}
    return {
        "arms": list(ALL_ARMS),
        "arms_paying_ingest": sorted(ARMS_WITH_INGEST & set(ALL_ARMS)),
        "datasets": rows,
        "total_questions": sum(r["questions"] for r in rows),
        "ingest_tokens": tot_ingest,
        "answer_tokens": tot_answer,
        "judge_tokens": tot_judge,
        "grand_total_tokens": grand_tok,
        "recommended_pair": f"{RECOMMENDED_ANSWER_MODEL} answerer + {RECOMMENDED_JUDGE_MODEL} judge",
        "recommended_usd": round(recommended_usd, 2),
        "usd_by_tier": tier_usd,
        "window_ingest_multiplier": WINDOW_INGEST_MULTIPLIER,
        "note": "FULL start-to-finish, ALL datasets x ALL arms. INGEST (pipeline "
                "reading each haystack into memory) DOMINATES and is paid only by "
                "base+neuron; rag ingests locally (~free). Ingest tokens INCLUDE the "
                f"windowing overhead (x{WINDOW_INGEST_MULTIPLIER}: an oversized haystack "
                "is read across ~N windowed LLM calls, each re-paying the prompt "
                "scaffold), so this is NOT just 'haystack size x questions' — it is "
                "the realistic figure. TOKENS are the invariant; $ depends on the "
                "models YOU pick (table + one RECOMMENDED worked example shown — you "
                "are free to choose any). Per-Q token counts are still a GUESS — run "
                "a small --sample first to measure the true windowed per-Q tokens.",
    }


def print_estimate_all(est: dict):
    e = lambda s: print(s, file=sys.stderr)
    e("\n=== FULL RUN ESTIMATE — ALL datasets x ALL arms (before any spend) ===")
    e(f"  arms: {', '.join(est['arms'])}  |  ingest paid by: "
      f"{', '.join(est['arms_paying_ingest'])}  (rag ingests locally ~free)")
    # --- per-dataset token table (TOKENS = the invariant headline) ---
    e(f"  {'dataset':13}{'Q':>6}{'ingest tok':>15}{'answer tok':>13}{'judge tok':>12}{'TOTAL tok':>15}")
    for r in est["datasets"]:
        e(f"  {r['dataset']:13}{r['questions']:>6}{r['ingest_tokens']:>15,}"
          f"{r['answer_tokens']:>13,}{r['judge_tokens']:>12,}{r['all_arms_tokens']:>15,}")
    e(f"  {'-'*74}")
    e(f"  {'GRAND TOTAL':13}{est['total_questions']:>6}{est['ingest_tokens']:>15,}"
      f"{est['answer_tokens']:>13,}{est['judge_tokens']:>12,}"
      f"{est['grand_total_tokens']:>15,}")
    e(f"  >>> {est['grand_total_tokens']:,} TOTAL TOKENS (~{est['grand_total_tokens']/1e6:.1f}M) "
      f"— ingest dominates ({est['ingest_tokens']/est['grand_total_tokens']*100:.0f}%)")
    # --- $ is model-dependent: a price table, YOU pick the model ---
    e("\n  $ COST (you choose the models — token count above is fixed; $ = tokens x price):")
    for name, usd in est["usd_by_tier"].items():
        e(f"    {name:42} ~${usd:,.0f}")
    e(f"  RECOMMENDED example ({est['recommended_pair']}): ~${est['recommended_usd']:,.0f}")
    e("  RECOMMENDATION: answerer + judge at frontier/mid tier, from DIFFERENT vendors")
    e("  (anthropic vs openai vs …) — never same-vendor both sides (self-scoring bias).")
    e("  You're free to pick any via --answer-model/--judge-model; this is a suggestion.")
    e("  NOTE: per-Q token counts are a FLOOR GUESS; run a small --sample first to "
      "measure real per-Q tokens before committing the full budget.")


def print_cost(est: dict):
    print("\n=== COST ESTIMATE (before any spend) ===", file=sys.stderr)
    print(f"  questions:      {est['n_questions']}", file=sys.stderr)
    print(f"  arm / dataset:  {est.get('arm','?')} / {est.get('dataset','?')}", file=sys.stderr)
    print(f"  answer model:   {est['answer_model']}", file=sys.stderr)
    print(f"  judge model:    {est['judge_model']}", file=sys.stderr)
    # TOKENS = the headline (real cost on subscription plans).
    # INGEST is the dominant term — show it FIRST so nobody is shocked. It's 0 for
    # the rag arm (local ingest) and includes the windowing overhead for base/neuron.
    _ing = est.get('est_ingest_tokens', 0)
    if _ing:
        _mult = est.get('window_ingest_multiplier', 1.0)
        print(f"  est. INGEST tok:{_ing:>10,}  <- DOMINANT "
              f"({est.get('est_ingest_tokens_per_q',0):,}/Q x{_mult} windowing)", file=sys.stderr)
    else:
        print(f"  est. INGEST tok:{0:>10,}  (this arm ingests locally ~free)", file=sys.stderr)
    print(f"  est. answer tok:{est['est_answer_tokens']:>10,}", file=sys.stderr)
    print(f"  est. judge  tok:{est['est_judge_tokens']:>10,}", file=sys.stderr)
    print(f"  EST. TOTAL TOK: {est['est_total_tokens']:>10,}", file=sys.stderr)
    if _ing:
        print(f"    >>> ingest is {_ing/est['est_total_tokens']*100:.0f}% of the bill "
              f"(the haystack read into memory, x{est.get('window_ingest_multiplier',1.0)} for windowing)", file=sys.stderr)
    # $ = footnote only (metered providers; ignore on flat-fee subs).
    print(f"  ($ footnote:    ${est['est_total_usd_low']}–${est['est_total_usd_high']} "
          f"metered-only, ignore on subs)", file=sys.stderr)
    print("  NOTE: per-Q ingest is a GUESS — run a small --sample first to measure the "
          "true windowed per-Q tokens before committing the full budget.", file=sys.stderr)
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


def _detect_default_arm(args) -> tuple[str, str]:
    """Pick the arm to run when the user didn't specify one: evaluate what they
    actually installed. Neuron is an UPGRADE LAYER, so if it's detectable, that's
    what they want measured; otherwise base. Side-effect-free (env + filesystem
    probe only, never executes the overlay). Returns (arm, reason)."""
    # 1. An explicit overlay command (env or .env) => neuron is set up for the bench.
    overlay = os.environ.get("DINOMEM_BENCH_OVERLAY_CMD", "").strip()
    if overlay:
        return "neuron", "DINOMEM_BENCH_OVERLAY_CMD is set"
    # 2. A neuron repo / installer locatable near the source workspace or CWD.
    src = (getattr(args, "source", "") or os.environ.get("DINOMEM_WORKSPACE", "")).strip()
    probes = []
    for root in (src, os.getcwd()):
        if not root:
            continue
        p = Path(root)
        # sibling/nested neuron repo layouts + an installed neuron marker
        probes += [
            p / "github" / "dinomem-neuron" / "scripts" / "install.sh",
            p.parent / "dinomem-neuron" / "scripts" / "install.sh",
            p / "procedures" / "memory_graph.py",   # neuron-only pipeline stage, installed
            p / "procedures" / "memory_synthesis.py",
        ]
    for probe in probes:
        try:
            if probe.exists():
                return "neuron", f"neuron detected ({probe})"
        except Exception:
            continue
    return "base", "no neuron overlay detected"

def main():
    ap = argparse.ArgumentParser(description="dinomem LongMemEval runner (one arm, end-to-end).")
    # --arm default is DYNAMIC: if the neuron upgrade layer is detectable on this
    # host, default to evaluating what the user actually installed (neuron); else
    # base. Explicit --arm always wins. (default=None -> resolved after parse.)
    ap.add_argument("--arm", choices=["rag", "base", "neuron"], default=None,
                    help="which arm to evaluate. Default: neuron if a neuron overlay "
                    "is detected (DINOMEM_BENCH_OVERLAY_CMD set, or a neuron repo/"
                    "install.sh found), else base. One arm per run; explicit wins.")
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
    ap.add_argument("--estimate-all", action="store_true",
                    help="print the FULL start-to-finish estimate (ALL datasets x "
                         "ALL arms) and EXIT. No --source needed, no spend.")
    args = ap.parse_args()

    # Resolve the dynamic --arm default: run what the user HAS. Neuron is an
    # upgrade layer, so its presence => they want it measured. Detection is cheap
    # and side-effect-free (env + filesystem probe only); never runs anything.
    if args.arm is None:
        detected, why = _detect_default_arm(args)
        args.arm = detected
        _log(f"--arm not given -> defaulting to '{detected}' ({why}). "
             f"Override with --arm base|neuron|rag.")

    # --estimate-all: pure arithmetic across every dataset+arm. Runs BEFORE the
    # --source requirement so anyone can price a full run with zero setup.
    if args.estimate_all:
        est = estimate_all(args.answer_model, args.judge_model)
        print_estimate_all(est)
        RESULTS.mkdir(parents=True, exist_ok=True)
        (RESULTS / "full_run_estimate.json").write_text(json.dumps(est, indent=2))
        print(json.dumps({"estimate_all": True, "estimate": est}, indent=2))
        sys.exit(0)

    mode_name = "full" if args.full else "sample"
    if args.full and not args.yes:
        _fail("--full is a paid citation-grade run; re-run with --yes to confirm "
              "(a cost estimate is printed first).")
    if not args.source:
        _fail("--source (or DINOMEM_WORKSPACE) required: the installed dinomem workspace")

    # cross-vendor guard: WARN (not block) if answerer+judge look same-vendor.
    _xv = cross_vendor_warning(args.answer_model, args.judge_model)
    if _xv:
        _log("WARNING: " + _xv)

    # rag + neuron both use the generic external-recall command hook; base uses
    # its lexical-over-distilled-memory path.
    recall = args.recall or ("base" if args.arm == "base" else "command")
    RESULTS.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    # ---- 0. cost estimate BEFORE any spend ----
    # N is exact for sample; for full we don't know until dataset loads, so estimate 500.
    est_n = args.n if mode_name == "sample" else 500
    est = cost_estimate(est_n, args.answer_model, args.judge_model,
                        arm=args.arm, dataset=getattr(args, "dataset", "longmemeval"))
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
            # rag arm does no pipeline drive, so re-derive the precise leak
            # signature (session-archive listings + dedup trackers) and compare.
            from setup_lab import _live_leak_signature as _leak_sig
            live_now = _leak_sig(Path(args.source)) if os.path.exists(args.source) else None
            live_then = lab_info.get("live_leak_sig")
            if live_then is not None and live_now is not None and live_now != live_then:
                _fail("ISOLATION BREACH: live session-archive listing or dedup "
                      "tracker changed during rag setup (a proc leaked into live)")
            _log("rag arm: no pipeline drive (retrieval over raw haystack); isolation OK")
            drive_res = {"ok": True, "arm": "rag", "note": "no-pipeline (naive RAG floor)"}
        elif args.arm == "neuron":
            drive_cmd = [sys.executable, HERE / "drive_neuron.py", "--ws", ws,
                         "--sandbox-root", lab,
                         "--live-source", args.source,
                         "--live-leak-sig", lab_info["live_leak_sig"], "--json"]
            drive_label = "drive_neuron"
        else:
            drive_cmd = [sys.executable, HERE / "drive_base.py", "--lab", lab,
                         "--live-source", args.source,
                         "--live-leak-sig", lab_info["live_leak_sig"], "--json"]
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
