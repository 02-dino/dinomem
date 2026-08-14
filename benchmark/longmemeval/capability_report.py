#!/usr/bin/env python3
"""
capability_report.py — TIER 2 capability track for the dinomem LongMemEval harness.

WHAT THIS IS (and is NOT):
  This is NOT a LongMemEval score. It is a set of self-contained, REPRODUCIBLE
  demonstrations — modelled exactly on the base/neuron README "how I know it works"
  sections — of capabilities the STANDARD LongMemEval benchmark structurally cannot
  measure. Each capability = one runnable scenario -> a real command -> the engine's
  own verbatim output. The reader can re-run every block and confirm it.

WHY SEPARATE (trust, non-negotiable):
  The Tier-1 comparison.md stays vanilla (unmodified engine, official scorer) so no
  skeptic can say "you tuned the benchmark". The engine-specific things neuron does
  that LongMemEval's single-shot QA can't see live HERE, clearly labelled
  "NOT LongMemEval-comparable". This is where dinomem shows depth without touching
  the citable headline number.

CAPABILITIES DEMONSTRATED (each built ON TOP of existing neuron tooling — this
script ORCHESTRATES + FORMATS, it does not reimplement any capability engine):
  (a) LEARNING / PROMOTION (L4)  — a fact promoted to _permanent.md survives and
        stays retrievable; explain_memory.py shows the promotion evidence +
        CALIBRATED confidence (calibrated vs an outcome ledger).
        tools: memory_promote.py (produce) + explain_memory.py --file (evidence)
  (b) TTL / TEMPORAL VALIDITY    — "moved Berlin 2024 then Tokyo 2025": bitemporal
        recall returns the AS-OF-date-correct answer, not just the latest.
        tools: hybrid_recall.py --as-of <ISO>  (+ valid_time.py underneath)
  (c) CONTRADICTION RESOLUTION   — conflicting statements in memory: the confidence
        surface carries contradictions[] and a resolved/higher-confidence pick.
        tools: contradiction_check.py + confidence_engine.py, surfaced via
               explain_memory.py --file --json (contradictions[] + calibrated_confidence)
  (d) RETRIEVAL PROVENANCE       — which leg surfaced a hit, at what rank/score:
        the engine's OWN auditable trace, not our narration.
        tools: kb/retrieval_log/*.jsonl (written by hybrid_recall.log_call)

DESIGN:
  - Runs against a WS that already has base+neuron installed + a driven pipeline
    (i.e. AFTER run.py --arm neuron has built + converged a lab, OR a caller passes
    a prepared --ws). This script does NOT install neuron or fetch LongMemEval; it
    is a capability DEMO harness, deliberately decoupled from the paid benchmark run.
  - Each probe: shell the real neuron command in the WS, capture stdout verbatim,
    emit a README-style ```command ... ``` + ```output ... ``` block.
  - Fail-open per-probe: a missing tool / empty surface is reported as
    "capability tool not present" rather than crashing — the report always renders.
  - NO LLM calls of its own unless a probe's underlying tool makes them (memory_promote
    is no-LLM; explain_memory is no-LLM; hybrid_recall's legs may embed locally). So
    this is effectively FREE to run vs the paid answer+judge benchmark.

USAGE:
  python3 capability_report.py --ws <NEURON_WS> [--out results/capability_report.md]
                               [--as-of 2025-01-01] [--permanent-file _permanent.md]
                               [--json]

  --ws  : a base+neuron workspace with a built pipeline (procedures/, kb/, memory/).
          For a live demo you can point it at a driven lab WS from run.py --arm neuron.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"

def _log(msg: str):
    print(f"[capability] {msg}", file=sys.stderr)

def _sh(cmd: list[str], ws: Path, timeout: int = 180) -> dict:
    """Run a neuron tool in the WS, capture verbatim stdout/stderr. Fail-open."""
    env = dict(os.environ)
    env["DINOMEM_WORKSPACE"] = str(ws)
    env["OPENCLAW_WORKSPACE"] = str(ws)
    env["PYTHONUNBUFFERED"] = "1"
    try:
        r = subprocess.run([str(c) for c in cmd], cwd=str(ws), env=env,
                           capture_output=True, text=True, timeout=timeout)
        return {"cmd": " ".join(str(c) for c in cmd), "rc": r.returncode,
                "stdout": (r.stdout or "").strip(), "stderr": (r.stderr or "").strip(),
                "ran": True, "timed_out": False}
    except subprocess.TimeoutExpired:
        return {"cmd": " ".join(str(c) for c in cmd), "rc": None,
                "stdout": "", "stderr": "TIMEOUT", "ran": True, "timed_out": True}
    except FileNotFoundError:
        return {"cmd": " ".join(str(c) for c in cmd), "rc": None,
                "stdout": "", "stderr": "tool not found", "ran": False, "timed_out": False}

def _tool(ws: Path, name: str) -> Path | None:
    for sub in ("procedures", "tools"):
        p = ws / sub / name
        if p.exists():
            return p
    return None

def _block(title: str, note: str, cmd_display: str, output: str,
           available: bool) -> dict:
    return {"title": title, "note": note, "command": cmd_display,
            "output": output, "available": available}

# ---------------------------------------------------------------------------
# PROBES — each returns a block dict. Fail-open: a missing tool -> available=False.
# ---------------------------------------------------------------------------
def probe_learning(ws: Path, permanent_file: str, timeout: int) -> dict:
    """L4 promotion: run memory_promote.py, then explain_memory.py --file <_permanent>
    to show the promotion evidence + calibrated confidence.
    """
    promote = _tool(ws, "memory_promote.py")
    explain = _tool(ws, "explain_memory.py")
    if not explain:
        return _block("Learning / promotion (L4)",
                      "explain_memory.py not present — capability tool missing.",
                      "python3 procedures/explain_memory.py --file memory/_permanent.md --json",
                      "", available=False)
    out_parts = []
    if promote:
        pr = _sh([sys.executable, promote], ws, timeout)
        out_parts.append(f"$ python3 procedures/memory_promote.py\n{pr['stdout'] or '(no stdout)'}")
    ex = _sh([sys.executable, explain, "--file", f"memory/{permanent_file}", "--json"],
             ws, timeout)
    out_parts.append(f"$ python3 procedures/explain_memory.py --file memory/{permanent_file} --json\n{ex['stdout'] or ex['stderr'] or '(no output)'}")
    return _block(
        "Learning / promotion (L4)",
        "A fact the engine judged durable is PROMOTED to _permanent.md; explain_memory "
        "shows the promotion evidence and the CALIBRATED confidence (calibrated against "
        "an outcome ledger, so 'trusted' means measured-reliable). LongMemEval is "
        "single-shot QA and cannot see cross-session consolidation.",
        "python3 procedures/memory_promote.py && \\\n"
        f"python3 procedures/explain_memory.py --file memory/{permanent_file} --json",
        "\n\n".join(out_parts),
        available=True)

def probe_ttl(ws: Path, as_of: str, timeout: int) -> dict:
    """Bitemporal recall: same query with and without --as-of shows the engine
    returning the as-of-date-correct answer, not just the latest fact.
    """
    hybrid = _tool(ws, "hybrid_recall.py")
    if not hybrid:
        return _block("TTL / temporal validity (bitemporal --as-of)",
                      "hybrid_recall.py not present — capability tool missing.",
                      "python3 tools/hybrid_recall.py \"where do I live\" --as-of <ISO> --json",
                      "", available=False)
    q = "where do I live"
    now = _sh([sys.executable, hybrid, q, "--k", "3", "--json"], ws, timeout)
    asof = _sh([sys.executable, hybrid, q, "--k", "3", "--as-of", as_of, "--json"], ws, timeout)
    output = (f"$ python3 tools/hybrid_recall.py \"{q}\" --k 3 --json   # current facts\n"
              f"{(now['stdout'] or now['stderr'] or '(no output)')[:1200]}\n\n"
              f"$ python3 tools/hybrid_recall.py \"{q}\" --k 3 --as-of {as_of} --json   # as-of {as_of}\n"
              f"{(asof['stdout'] or asof['stderr'] or '(no output)')[:1200]}")
    return _block(
        "TTL / temporal validity (bitemporal --as-of)",
        "Given a fact that CHANGED over time (e.g. moved Berlin 2024 -> Tokyo 2025), "
        f"the same query returns the CURRENT answer by default but the AS-OF-{as_of} "
        "answer when time-anchored. This bitemporal validity is neuron's real edge; "
        "LongMemEval's temporal category only partially touches it.",
        f"# default = current fact\npython3 tools/hybrid_recall.py \"{q}\" --k 3 --json\n"
        f"# time-anchored = fact valid at {as_of}\n"
        f"python3 tools/hybrid_recall.py \"{q}\" --k 3 --as-of {as_of} --json",
        output, available=True)

def probe_contradiction(ws: Path, timeout: int) -> dict:
    """Contradiction resolution: run contradiction_check + confidence_engine, then
    surface the contradictions[] + calibrated_confidence via explain_memory.
    """
    contra = _tool(ws, "contradiction_check.py")
    conf = _tool(ws, "confidence_engine.py")
    explain = _tool(ws, "explain_memory.py")
    if not explain:
        return _block("Contradiction resolution",
                      "explain_memory.py not present — capability tool missing.",
                      "python3 procedures/explain_memory.py --query \"\" --json",
                      "", available=False)
    out_parts = []
    if contra:
        cr = _sh([sys.executable, contra], ws, timeout)
        out_parts.append(f"$ python3 procedures/contradiction_check.py\n{cr['stdout'] or '(no stdout)'}")
    if conf:
        cf = _sh([sys.executable, conf], ws, timeout)
        out_parts.append(f"$ python3 procedures/confidence_engine.py\n{cf['stdout'] or '(no stdout)'}")
    # explain across all queries surfaces which files carry contradictions[].
    ex = _sh([sys.executable, explain, "--query", "", "--json"], ws, timeout)
    out_parts.append(f"$ python3 procedures/explain_memory.py --query \"\" --json\n{(ex['stdout'] or ex['stderr'] or '(no output)')[:1400]}")
    return _block(
        "Contradiction resolution",
        "When memory holds conflicting statements, contradiction_check flags them and "
        "confidence_engine deflates the confidence of the loser; explain_memory surfaces "
        "contradictions[] and the calibrated_confidence of the survivor. LongMemEval "
        "does not probe conflict resolution as a first-class axis.",
        "python3 procedures/contradiction_check.py && \\\n"
        "python3 procedures/confidence_engine.py && \\\n"
        "python3 procedures/explain_memory.py --query \"\" --json",
        "\n\n".join(out_parts),
        available=True)

def probe_provenance(ws: Path, timeout: int) -> dict:
    """Retrieval provenance: read the engine's own retrieval log — which leg
    surfaced each hit, at what rank/score. Auditable trace, not narration.
    """
    log_dir = ws / "kb" / "retrieval_log"
    explain = _tool(ws, "explain_memory.py")
    logs = sorted(log_dir.glob("*.jsonl")) if log_dir.exists() else []
    if not logs and not explain:
        return _block("Retrieval provenance",
                      "no kb/retrieval_log/*.jsonl and no explain_memory.py — run some "
                      "hybrid_recall queries first to populate the log.",
                      "cat kb/retrieval_log/*.jsonl | tail",
                      "", available=False)
    out_parts = []
    if logs:
        tail = []
        try:
            lines = logs[-1].read_text(encoding="utf-8").splitlines()
            tail = lines[-8:]
        except Exception as e:  # noqa: BLE001
            tail = [f"(could not read {logs[-1].name}: {e})"]
        out_parts.append(f"$ tail -8 kb/retrieval_log/{logs[-1].name}\n" + "\n".join(tail))
    if explain:
        ex = _sh([sys.executable, explain, "--query", "", "--json"], ws, timeout)
        out_parts.append(f"$ python3 procedures/explain_memory.py --query \"\" --json\n{(ex['stdout'] or '(no output)')[:1000]}")
    return _block(
        "Retrieval provenance (auditable trace)",
        "Every hybrid_recall query logs which leg (docs/session/graph/memory) surfaced "
        "each candidate, at what rank and score, into kb/retrieval_log/. explain_memory "
        "joins that with lifecycle + confidence. This is the engine's OWN audit trail — "
        "the 'Explainable + calibrated' differentiator the neuron README leads with — not "
        "a claim we assert. LongMemEval reports a score with no provenance at all.",
        "tail kb/retrieval_log/*.jsonl   # per-query leg + rank + score\n"
        "python3 procedures/explain_memory.py --query \"\" --json",
        "\n\n".join(out_parts),
        available=True)

# ---------------------------------------------------------------------------
def render_md(blocks: list[dict], ws: Path, as_of: str) -> str:
    lines = [
        "# dinomem — Capability Report (Tier 2)",
        "",
        "> **NOT LongMemEval-comparable.** These are reproducible demonstrations of "
        "capabilities the standard LongMemEval benchmark cannot measure. The citable "
        "LongMemEval-S score lives in `comparison.md` (Tier 1, unmodified engine). This "
        "document shows what the **neuron** layer does *beyond* what a single-shot QA "
        "benchmark can see. Every block below is a real command against a base+neuron "
        "workspace and its verbatim output — re-run them to verify.",
        "",
        f"_Generated: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())} · "
        f"workspace: `{ws}` · as-of demo date: `{as_of}`_",
        "",
        "---",
        "",
    ]
    for i, b in enumerate(blocks, 1):
        lines.append(f"## {i}. {b['title']}")
        lines.append("")
        if not b["available"]:
            lines.append(f"> ⚠️ _Not demonstrated:_ {b['note']}")
            lines.append("")
            continue
        lines.append(b["note"])
        lines.append("")
        lines.append("**Command:**")
        lines.append("```bash")
        lines.append(b["command"])
        lines.append("```")
        lines.append("")
        lines.append("**Output (verbatim):**")
        lines.append("```")
        lines.append(b["output"] or "(no output captured)")
        lines.append("```")
        lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("_Tier 2 is deliberately decoupled from the paid benchmark run. It "
                 "orchestrates existing neuron tools (memory_promote, explain_memory, "
                 "hybrid_recall, contradiction_check, confidence_engine) and formats their "
                 "real output; it introduces no new scoring and makes no LongMemEval claim._")
    return "\n".join(lines) + "\n"

def main():
    ap = argparse.ArgumentParser(description="dinomem Tier-2 capability report (README 'how I know it works' style).")
    ap.add_argument("--ws", required=True, help="base+neuron workspace with a built pipeline")
    ap.add_argument("--out", default=str(RESULTS / "capability_report.md"),
                    help="output markdown (default: results/capability_report.md)")
    ap.add_argument("--as-of", default="2025-01-01", help="ISO date for the bitemporal TTL demo")
    ap.add_argument("--permanent-file", default="_permanent.md",
                    help="promoted-memory filename under memory/ (default _permanent.md)")
    ap.add_argument("--timeout", type=int, default=180, help="per-probe timeout seconds")
    ap.add_argument("--json", action="store_true", help="also emit machine-readable JSON")
    args = ap.parse_args()

    ws = Path(args.ws).resolve()
    if not ws.exists():
        print(f"[capability] FAIL: workspace not found: {ws}", file=sys.stderr)
        sys.exit(2)
    if not (ws / "procedures").exists():
        print(f"[capability] FAIL: {ws} has no procedures/ (not a dinomem workspace)", file=sys.stderr)
        sys.exit(2)

    _log(f"probing capabilities in {ws}")
    blocks = [
        probe_learning(ws, args.permanent_file, args.timeout),
        probe_ttl(ws, args.as_of, args.timeout),
        probe_contradiction(ws, args.timeout),
        probe_provenance(ws, args.timeout),
    ]
    md = render_md(blocks, ws, args.as_of)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    _log(f"wrote {out}")

    n_avail = sum(1 for b in blocks if b["available"])
    if args.json:
        print(json.dumps({"out": str(out), "probes": len(blocks),
                          "available": n_avail,
                          "titles": [b["title"] for b in blocks]}, indent=2))
    else:
        print(f"[capability] wrote {out} — {n_avail}/{len(blocks)} capabilities demonstrated")
    # Non-fatal if some probes couldn't run (fail-open); exit 0 as long as report wrote.
    sys.exit(0)

if __name__ == "__main__":
    main()
