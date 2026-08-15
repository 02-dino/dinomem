#!/usr/bin/env python3
"""
compare_report.py — the 3-ARM LongMemEval comparison report.

Reads each arm's machine-readable result (results/<arm>_result.json, emitted by
run.py) and writes results/comparison.md: side-by-side accuracy + the FULL 1b
metric set (retrieval recall/precision, tokens, latency, storage) across arms,
with deltas vs the RAG floor.

THE ARMS (the single variable is the memory engine):
  ARM rag    = plain vector RAG floor (no dinomem pipeline). The naive competitor.
  ARM base   = dinomem base pipeline (public floor).
  ARM neuron = base + neuron overlay (neuron is an UPGRADE LAYER, never standalone).
  Deltas are shown vs RAG (does the pipeline beat naive retrieval?) and base
  (does the neuron layer add on top of base?).

FAIRNESS GATE (refuse to render an invalid comparison — fail loud):
  A cross-arm delta is only meaningful if EVERYTHING except the engine is
  identical. Each arm result carries a SHARED-PROTOCOL HASH (run.py protocol_hash
  over dataset line+SHA, answer model, judge, sample N, protocol version). We
  HARD-REFUSE if the arms' protocol_hash values differ — that single check
  subsumes the old per-knob checks, and also catches a stale-protocol arm. When a
  hash mismatch is found we ALSO print the specific differing knob(s) so the fix
  is obvious. We refuse to anchor on a NONDETERMINISTIC arm unless overridden.

USAGE:
  # default: compare all three arms if their result files exist
  python3 compare_report.py [--arms rag,base,neuron] \\
      [--out results/comparison.md] [--allow-nondeterministic] [--json]
  # subset also fine (e.g. just base vs neuron):
  python3 compare_report.py --arms base,neuron
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent.resolve()
RESULTS = HERE / "results"
LEADERBOARD = "https://github.com/xiaowu0162/LongMemEval"
CATEGORIES = [
    "single-session-user", "single-session-preference", "single-session-assistant",
    "multi-session", "temporal-reasoning", "knowledge-update",
]
ARM_LABEL = {
    "rag": "RAG floor",
    "base": "base",
    "neuron": "base+neuron",
}


def _fail(msg: str, code: int = 2):
    print(f"[compare] FAIL: {msg}", file=sys.stderr)
    sys.exit(code)


def _load(path: Path) -> dict:
    if not path.exists():
        _fail(f"arm result not found: {path} (run.py --arm <...> must produce it first)")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        _fail(f"cannot parse {path}: {e}")


# ── fairness: the shared-protocol hash is the single source of truth ─────────
def _protocol_knobs(r: dict) -> dict:
    d = r.get("dataset", {})
    return {
        "protocol_version": r.get("protocol_version"),
        "dataset_line": d.get("line"),
        "dataset_revision": d.get("revision"),
        "dataset_sha256": d.get("sha256"),
        "answer_model": r.get("answer_model"),
        "judge_model": r.get("judge_model"),
        "mode": r.get("mode"),
        "n": r.get("n"),
    }


def fairness_gate(results: dict, allow_nondet: bool) -> list[str]:
    """results: {arm -> result dict}. Returns blocking problems; empty = fair."""
    problems: list[str] = []
    arms = list(results)
    ref_arm = arms[0]
    ref_hash = results[ref_arm].get("protocol_hash")
    ref_knobs = _protocol_knobs(results[ref_arm])

    for arm in arms[1:]:
        h = results[arm].get("protocol_hash")
        if h is None or ref_hash is None:
            problems.append(
                f"MISSING protocol_hash on arm '{arm if h is None else ref_arm}' "
                "(re-run with the current run.py that stamps protocol_hash)")
            continue
        if h != ref_hash:
            # surface the specific differing knob(s)
            knobs = _protocol_knobs(results[arm])
            diffs = [f"{k}: {ref_arm}={ref_knobs[k]!r} vs {arm}={knobs[k]!r}"
                     for k in ref_knobs if ref_knobs[k] != knobs[k]]
            detail = "; ".join(diffs) or "hash differs but knobs look equal (protocol_version?)"
            problems.append(
                f"PROTOCOL mismatch {ref_arm} vs {arm}: {detail} "
                "(arms must share dataset/SHA + answer model + judge + N + protocol version)")

    if not allow_nondet:
        for arm, r in results.items():
            det = r.get("determinism")
            if det and det.get("verdict") == "NONDETERMINISTIC":
                problems.append(
                    f"'{arm}' arm flagged NONDETERMINISTIC "
                    f"(run1={det.get('overall_1')} run2={det.get('overall_2')}); "
                    "refuse to anchor a comparison on a shaky number "
                    "(override with --allow-nondeterministic)")
    return problems


# ── formatting helpers ───────────────────────────────────────────────────────
def _fmt(x, nd=4):
    if x is None:
        return "—"
    if isinstance(x, float):
        return f"{x:.{nd}f}"
    return str(x)


def _delta_str(a, b):
    if a is None or b is None:
        return "—"
    d = round(b - a, 4)
    sign = "+" if d > 0 else ""
    return f"{sign}{d:.4f}"


def _human_bytes(n):
    if n is None:
        return "—"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n/1024**(('B','KB','MB','GB').index(unit)):.1f}{unit}"
        n0 = n
    return f"{n}B"


def _bytes_str(n):
    if n is None:
        return "—"
    if n < 1024:
        return f"{n}B"
    if n < 1024**2:
        return f"{n/1024:.1f}KB"
    return f"{n/1024**2:.2f}MB"


def _m(r):  # arm metrics
    return r.get("metrics", {}) or {}


def _get(r, *path, default=None):
    cur = _m(r)
    for p in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(p)
        if cur is None:
            return default
    return cur


def _row(label, results, arms, getter, fmt=_fmt, delta_base="rag"):
    """One metric row across all arms, with a delta vs delta_base arm."""
    vals = {a: getter(results[a]) for a in arms}
    cells = " | ".join(fmt(vals[a]) for a in arms)
    dcell = ""
    if delta_base in arms and len(arms) > 1:
        # delta of the LAST arm vs the delta_base arm (most-upgraded vs floor)
        last = arms[-1]
        if last != delta_base:
            dcell = f" | {_delta_str(vals[delta_base], vals[last])}"
    return f"| {label} | {cells}{dcell} |"


def build_md(results: dict, arms: list[str]) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    ref = results[arms[0]]
    d = ref.get("dataset", {})
    bm = _m(ref)

    arm_hdr = " | ".join(f"{a} ({ARM_LABEL.get(a, a)})" for a in arms)
    delta_col = ""
    if "rag" in arms and arms[-1] != "rag":
        delta_col = f" | Δ ({arms[-1]}−rag)"
    elif len(arms) > 1:
        delta_col = f" | Δ ({arms[-1]}−{arms[0]})"
    delta_ref = "rag" if "rag" in arms else arms[0]

    def acc(cat):
        return lambda r: _get(r, "per_category", cat, "accuracy")

    lines = [
        "# LongMemEval-S — dinomem 3-arm comparison",
        "",
        "All arms run the **identical harness protocol** (verified by a shared "
        "protocol hash). The **only variable is the memory engine**:",
        "",
        "- **rag** — plain vector RAG floor (no dinomem pipeline): the naive competitor.",
        "- **base** — dinomem base pipeline.",
        "- **neuron** — base **+ neuron overlay** (upgrade layer).",
        "",
        f"Δ column = **{arms[-1]} − {delta_ref}** "
        "(does the more-engineered engine beat the floor?).",
        "",
        "## Accuracy",
        "",
        f"| Metric | {arm_hdr}{delta_col} |",
        "|" + "-|" * (len(arms) + 1 + (1 if delta_col else 0)),
        _row("Overall accuracy", results, arms,
             lambda r: _get(r, "overall_accuracy"), delta_base=delta_ref),
        _row("Task-averaged accuracy", results, arms,
             lambda r: _get(r, "task_averaged_accuracy"), delta_base=delta_ref),
        _row("Abstention accuracy", results, arms,
             lambda r: _get(r, "abstention_accuracy"), delta_base=delta_ref),
        "",
        "### Per-category accuracy",
        "",
        f"| Category | {arm_hdr}{delta_col} |",
        "|" + "-|" * (len(arms) + 1 + (1 if delta_col else 0)),
    ]
    for cat in CATEGORIES:
        lines.append(_row(cat, results, arms, acc(cat), delta_base=delta_ref))

    # ── the 1b metric set: retrieval / cost / latency / storage ──
    lines += [
        "",
        "## Retrieval quality (vs gold answer-sessions)",
        "",
        "> Null where an engine surfaces no session-level attribution — base "
        "recall returns distilled memory *items* (file-sourced), not sessions, so "
        "its recall/precision are honestly null. RAG/neuron retrieve "
        "session-attributable evidence. That asymmetry is itself a finding.",
        "",
        f"| Metric | {arm_hdr}{delta_col} |",
        "|" + "-|" * (len(arms) + 1 + (1 if delta_col else 0)),
        _row("Recall@k", results, arms,
             lambda r: _get(r, "retrieval", "recall_at_k"), delta_base=delta_ref),
        _row("Precision@k", results, arms,
             lambda r: _get(r, "retrieval", "precision_at_k"), delta_base=delta_ref),
        _row("Avg retrieved count", results, arms,
             lambda r: _get(r, "retrieval", "avg_retrieved_count"),
             fmt=lambda x: _fmt(x, 2), delta_base=delta_ref),
        _row("Attributable questions", results, arms,
             lambda r: _get(r, "retrieval", "attributable_questions"),
             fmt=_fmt, delta_base=delta_ref),
        "",
        "## Cost (context + tokens)",
        "",
        f"| Metric | {arm_hdr} |",
        "|" + "-|" * (len(arms) + 1),
        _row("Avg context chars", results, arms,
             lambda r: _get(r, "cost", "avg_context_chars"),
             fmt=lambda x: _fmt(x, 1), delta_base="__none__"),
        _row("Avg prompt tokens", results, arms,
             lambda r: _get(r, "cost", "avg_prompt_tokens"),
             fmt=lambda x: _fmt(x, 1), delta_base="__none__"),
        _row("Avg completion tokens", results, arms,
             lambda r: _get(r, "cost", "avg_completion_tokens"),
             fmt=lambda x: _fmt(x, 1), delta_base="__none__"),
        _row("Avg total tokens/Q", results, arms,
             lambda r: _get(r, "cost", "avg_total_tokens"),
             fmt=lambda x: _fmt(x, 1), delta_base="__none__"),
        _row("Sum total tokens", results, arms,
             lambda r: _get(r, "cost", "sum_total_tokens"),
             fmt=_fmt, delta_base="__none__"),
        "",
        "## Latency",
        "",
        f"| Metric | {arm_hdr} |",
        "|" + "-|" * (len(arms) + 1),
        _row("Avg recall ms", results, arms,
             lambda r: _get(r, "latency", "avg_recall_ms"),
             fmt=lambda x: _fmt(x, 1), delta_base="__none__"),
        _row("Avg answer ms", results, arms,
             lambda r: _get(r, "latency", "avg_answer_ms"),
             fmt=lambda x: _fmt(x, 1), delta_base="__none__"),
        "",
        "## Storage (memory + index footprint)",
        "",
        f"| Metric | {arm_hdr} |",
        "|" + "-|" * (len(arms) + 1),
        _row("Total footprint", results, arms,
             lambda r: _get(r, "storage", "total_bytes"),
             fmt=_bytes_str, delta_base="__none__"),
        _row("Memory footprint", results, arms,
             lambda r: _get(r, "storage", "memory_bytes"),
             fmt=_bytes_str, delta_base="__none__"),
        _row("RAG index footprint", results, arms,
             lambda r: _get(r, "storage", "rag_index_bytes"),
             fmt=_bytes_str, delta_base="__none__"),
    ]

    # ── shared stamp + provenance ──
    lines += [
        "",
        "## Shared run stamp (identical across all arms — that's the point)",
        "",
        f"- **Protocol hash:** `{ref.get('protocol_hash')}`  "
        f"(version {ref.get('protocol_version')})",
        f"- **Dataset line:** {d.get('line')}  ·  **file:** {d.get('file')}",
        f"- **HF revision SHA:** {d.get('revision')}",
        f"- **Dataset sha256:** {d.get('sha256')}  (verified={d.get('hash_verified')})",
        f"- **Answer model:** {ref.get('answer_model')}",
        f"- **Judge model:** {ref.get('judge_model')} "
        f"(canonical={bm.get('canonical_judge')})",
        f"- **Mode / N:** {ref.get('mode')} / {ref.get('n')}",
        "",
        "## Per-arm determinism + wall time",
        "",
    ]
    for a in arms:
        r = results[a]
        det = r.get("determinism")
        secs = r.get("seconds")
        if det:
            lines.append(f"- **{a}:** {det.get('verdict')} "
                         f"(run1={det.get('overall_1')} run2={det.get('overall_2')} "
                         f"drift={det.get('drift')})  ·  {secs}s")
        else:
            lines.append(f"- **{a}:** single pass (determinism guard not run)  ·  {secs}s")

    lines += [
        "",
        "---",
        f"_Generated {now}. Official leaderboard / paper: {LEADERBOARD}_",
        "",
        "> Absolute numbers are leaderboard-comparable only when the dataset line + "
        "judge (gpt-4o-class) + hypothesis format match the convention. Cross-arm "
        "DELTAS are valid regardless (same judge for all arms). See each arm's "
        "`*_latest.md` for its full disclosed stamp.",
        "",
    ]
    if not bm.get("canonical_judge"):
        lines.insert(4, "> ⚠️ **Non-canonical judge** — cross-arm deltas are valid "
                        "(all arms share the judge), but absolute numbers are not "
                        "leaderboard-canonical. Use a gpt-4o-class judge for citable absolutes.\n")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="dinomem 3-arm LongMemEval comparison report.")
    ap.add_argument("--arms", default="rag,base,neuron",
                    help="comma-separated arms to compare (subset ok); "
                    "each needs results/<arm>_<dataset>_result.json (or legacy <arm>_result.json)")
    ap.add_argument("--dataset", default="longmemeval", choices=["longmemeval", "locomo"],
                    help="which dataset's per-arm results to compare (result files are "
                    "tagged {arm}_{dataset}; falls back to legacy {arm} for old artifacts).")
    ap.add_argument("--out", default=str(RESULTS / "comparison.md"))
    ap.add_argument("--allow-nondeterministic", action="store_true",
                    help="override the determinism refuse-gate (NOT recommended)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    if len(arms) < 2:
        _fail("need >=2 arms to compare (e.g. --arms rag,base,neuron)")

    results = {}
    for a in arms:
        # prefer the dataset-tagged file; fall back to the legacy arm-only name so
        # pre-tagging result artifacts still compare.
        tagged = RESULTS / f"{a}_{args.dataset}_result.json"
        legacy = RESULTS / f"{a}_result.json"
        results[a] = _load(tagged if tagged.exists() else legacy)

    problems = fairness_gate(results, args.allow_nondeterministic)
    if problems:
        print("[compare] FAIRNESS GATE TRIPPED — refusing to render an invalid comparison:",
              file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        print("[compare] Fix the mismatched knob and re-run the arms with the same "
              "protocol, then regenerate.", file=sys.stderr)
        sys.exit(3)

    md = build_md(results, arms)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    print(f"[compare] wrote {out} ({len(arms)} arms: {', '.join(arms)})", file=sys.stderr)
    if args.json:
        summary = {"out": str(out), "arms": arms, "fair": True}
        for a in arms:
            summary[f"{a}_overall"] = _get(results[a], "overall_accuracy")
        print(json.dumps(summary, indent=2))
    else:
        print(md)


if __name__ == "__main__":
    main()
