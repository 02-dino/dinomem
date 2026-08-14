#!/usr/bin/env python3
"""
authority_run.py — Phase 5c: AUTHORITY-SCOPE / UNTRUSTED-INSTRUCTION runner (one arm).

Imports mem_authority.py from the arm's procedures/ (base OR neuron overlay) and
calls the write-side gate on each labeled case, then grades the outcome against
gold. DIRECT-CALL, no LLM, no lab archive -> FREE for every arm that ships the gate.

WHAT IT PROVES: the write-side gate that makes provenance (WHO) ⊥ authority (WHAT):
  non-owner directive  -> DROPPED (world lane) / DEMOTED (peer lane), never obeyable
  non-owner self-fact  -> KEPT verbatim (personalization preserved)
  owner directive      -> KEPT (owner may set standing rules)

ARM MODEL:
  base   -> base's mem_authority.py.   neuron -> neuron's (overlay may enhance it).
  rag    -> has no gate -> reported authority_supported=False (unsupported, N/A).
  The base-vs-neuron delta = whatever the neuron overlay improves in the gate.

METRICS:
  directive_block_rate       — non-owner directives correctly dropped/demoted (HEADLINE, higher=safer)
  personalization_keep_rate  — non-owner self-facts correctly kept verbatim
  owner_passthrough_rate     — owner items passed untouched
  false_block_rate           — non-owner self-facts WRONGLY blocked/demoted (over-filter, lower=better)
  scope_accuracy             — classify_scope() label accuracy vs gold scope

USAGE:
  python3 authority_run.py --arm base|neuron|rag --cases cases.json \
      --source <WS_with_procedures> [--out results/authority_<arm>.json]
"""
from __future__ import annotations
import argparse
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]           # benchmark/
RESULTS = ROOT / "authority" / "results"


def _log(m: str) -> None:
    print(m, file=sys.stderr, flush=True)


def _load_gate(source_ws: Path):
    """Import mem_authority.py from the arm's procedures/. Returns module or None."""
    cand = source_ws / "procedures" / "mem_authority.py"
    if not cand.exists():
        # fall back: maybe source_ws already IS procedures/, or flat layout
        alt = source_ws / "mem_authority.py"
        cand = alt if alt.exists() else cand
    if not cand.exists():
        return None
    try:
        spec = importlib.util.spec_from_file_location("mem_authority_arm", str(cand))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        # sanity: the three public entry points must exist
        for fn in ("classify_scope", "gate_peer_fact", "gate_world_fact"):
            if not hasattr(mod, fn):
                return None
        return mod
    except Exception as e:  # noqa: BLE001
        _log(f"  mem_authority import failed: {e}")
        return None


def _gate_outcome(mod, case) -> str:
    """Run the arm's gate for a case; map to keep|demote|drop."""
    is_owner_src = (case["source"] == "owner")
    if case["lane"] == "peer":
        keep, out_text, demoted = mod.gate_peer_fact(case["text"], is_owner_src)
        if not keep:
            return "drop"
        return "demote" if demoted else "keep"
    # world lane
    keep, out_text, demoted = mod.gate_world_fact(case["text"], is_owner_src)
    if not keep:
        return "drop"
    return "demote" if demoted else "keep"


def run_arm(args) -> dict:
    RESULTS.mkdir(parents=True, exist_ok=True)
    spec = json.loads(Path(args.cases).read_text())
    cases = spec["cases"]
    t0 = time.time()

    if args.arm == "rag":
        result = {"ok": True, "arm": "rag", "schema": spec["schema"],
                  "authority_supported": False,
                  "note": "rag arm has no write-side authority gate (no mem_authority.py); "
                          "authority metrics N/A. base/neuron ship the gate."}
        out = Path(args.out) if args.out else RESULTS / f"authority_{args.arm}.json"
        out.write_text(json.dumps(result, indent=2), encoding="utf-8")
        result["out"] = str(out)
        return result

    source_ws = Path(args.source or os.environ.get("DINOMEM_WORKSPACE", "")).expanduser()
    mod = _load_gate(source_ws)
    if mod is None:
        result = {"ok": False, "arm": args.arm, "schema": spec["schema"],
                  "authority_supported": False,
                  "reason": f"mem_authority.py not importable from {source_ws}/procedures"}
        out = Path(args.out) if args.out else RESULTS / f"authority_{args.arm}.json"
        out.write_text(json.dumps(result, indent=2), encoding="utf-8")
        result["out"] = str(out)
        return result

    # tally buckets
    dir_total = dir_blocked = 0            # non-owner directives (should drop/demote)
    pers_total = pers_kept = pers_false = 0  # non-owner personalization (should keep)
    owner_total = owner_pass = 0           # owner (should keep)
    scope_correct = 0
    per_case = []

    for c in cases:
        got = _gate_outcome(mod, c)
        want = c["outcome"]
        scope_got = mod.classify_scope(c["text"])
        if scope_got == c["scope"]:
            scope_correct += 1
        correct = (got == want)

        is_owner_src = (c["source"] == "owner")
        if is_owner_src:
            owner_total += 1
            if got == "keep":
                owner_pass += 1
        elif c["scope"] == "directive":
            dir_total += 1
            # correct block = dropped (world) or demoted (peer)
            if got in ("drop", "demote"):
                dir_blocked += 1
        else:  # non-owner personalization
            pers_total += 1
            if got == "keep":
                pers_kept += 1
            else:
                pers_false += 1  # wrongly blocked/demoted a self-fact

        per_case.append({"id": c["id"], "source": c["source"], "lane": c["lane"],
                         "scope": c["scope"], "want": want, "got": got,
                         "scope_got": scope_got, "correct": correct})

    def _rate(n, d):
        return round(100 * n / d, 1) if d else None

    result = {
        "ok": True, "arm": args.arm, "schema": spec["schema"],
        "authority_supported": True,
        "n_cases": len(cases),
        "directive_block_rate": _rate(dir_blocked, dir_total),       # HEADLINE
        "directive_n": dir_total,
        "personalization_keep_rate": _rate(pers_kept, pers_total),
        "personalization_n": pers_total,
        "false_block_rate": _rate(pers_false, pers_total),           # lower=better
        "owner_passthrough_rate": _rate(owner_pass, owner_total),
        "owner_n": owner_total,
        "scope_accuracy": _rate(scope_correct, len(cases)),
        "per_case": per_case,
        "seconds": round(time.time() - t0, 2),
        "note": "direct-call gate eval (no LLM). directive_block_rate is the PDF §11 "
                "headline: non-owner system-directives must not become obeyable rules.",
    }
    _log(f"  directive_block={result['directive_block_rate']}% "
         f"pers_keep={result['personalization_keep_rate']}% "
         f"owner_pass={result['owner_passthrough_rate']}% "
         f"false_block={result['false_block_rate']}% scope_acc={result['scope_accuracy']}%")
    out = Path(args.out) if args.out else RESULTS / f"authority_{args.arm}.json"
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    result["out"] = str(out)
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description="Phase 5c authority-scope runner (one arm)")
    ap.add_argument("--arm", choices=["rag", "base", "neuron"], default="neuron")
    ap.add_argument("--cases", required=True)
    ap.add_argument("--source", default=os.environ.get("DINOMEM_WORKSPACE", ""))
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    res = run_arm(args)
    print(json.dumps({k: v for k, v in res.items() if k != "per_case"}, indent=2))


if __name__ == "__main__":
    main()
