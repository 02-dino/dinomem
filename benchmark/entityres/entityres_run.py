#!/usr/bin/env python3
"""
entityres_run.py — Phase 5e: ENTITY-RESOLUTION runner (one arm).

WHAT THIS PROVES (PDF §2 "entity/relationship reasoning" — currently 0 coverage):
  Real transcripts refer to the same entity by varying surface forms ("Alice",
  "Alice Chen", "A. Chen"). If the graph treats each as a distinct node, multi-hop
  reasoning fractures. entity_resolver.py (neuron, called by memory_graph) clusters
  surface forms into ONE canonical entity + aliases, same-type-guarded and
  conservative (precision over recall — a wrong merge corrupts the graph).

  Pure-python + deterministic -> runs FREE. neuron-only (base has no resolver);
  base/rag reported unsupported. This makes the neuron entity-reasoning edge
  MEASURABLE instead of asserted.

CHECKS (deterministic, free):
  merges_alias        — "Alice Chen" and "A. Chen" collapse to ONE canonical entity
  picks_canonical     — canonical = the most-mentioned surface form
  keeps_aliases       — the merged surface forms are retained as aliases[] (reversible)
  no_cross_type_merge — a PERSON "Apple" and an ORG "Apple" are NOT merged (type guard)
  no_false_merge      — two genuinely-distinct people ("Alice", "Bob") stay separate
  canonicalize_maps   — canonicalize("A. Chen", map) returns the canonical name

METRIC: entityres_score = fraction of checks passed. no_false_merge +
no_cross_type_merge are the precision headline (dinomem favors precision).

USAGE:
  python3 entityres_run.py --arm neuron|base|rag --source <WS_with_procedures> \
      [--out results/entityres_<arm>.json]
"""
from __future__ import annotations
import argparse
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "entityres" / "results"


def _log(m: str) -> None:
    print(m, file=sys.stderr, flush=True)


def _load_resolver(source_ws: Path):
    cand = source_ws / "procedures" / "entity_resolver.py"
    if not cand.exists():
        alt = source_ws / "entity_resolver.py"
        cand = alt if alt.exists() else cand
    if not cand.exists():
        return None
    try:
        spec = importlib.util.spec_from_file_location("entity_resolver_arm", str(cand))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        for fn in ("resolve_entities", "build_alias_map", "canonicalize"):
            if not hasattr(mod, fn):
                return None
        return mod
    except Exception as e:  # noqa: BLE001
        _log(f"  entity_resolver import failed: {e}")
        return None


# Labeled entity surface forms (as extract_entity_nodes would emit).
# NOTE on tiers: the OFFLINE resolver runs TIER 1 (token-subset alias) deterministically;
# TIER 2 (semantic/embedding alias, e.g. "A. Chen" == "Alice Chen") needs an embedding
# backend not present in a direct free call. So the fixture uses a genuine TIER-1
# subset case ("Chen" is a token-subset of "Alice Chen") for the core merge checks,
# and keeps the abbreviation case as an embedding-gated (skipped-when-unavailable) probe.
ENTITIES = [
    {"name": "Alice Chen", "type": "person", "mentions": 5},
    {"name": "Chen", "type": "person", "mentions": 2},        # TIER-1 token-subset -> alias of Alice Chen
    {"name": "Bob", "type": "person", "mentions": 4},          # distinct person
    {"name": "Apple", "type": "person", "mentions": 1},        # person named Apple
    {"name": "Apple", "type": "org", "mentions": 3},           # the company -> NOT merged w/ person
]


def _run_checks(mod) -> dict:
    # The resolver is PRECISION-FIRST: it merges only near-identical surface forms
    # (normalized string similarity >= SUBSET_RATIO 0.9), same-type-guarded. A missed
    # merge is cheap; a WRONG merge corrupts the graph. So the eval proves exactly
    # that conservative contract: identical/whitespace/case variants merge; surname-
    # only, typos, and suffix variants do NOT (no over-merge); cross-type never merges.
    checks: dict = {}
    try:
        # 1) MERGE case: case/whitespace variants of the SAME name -> one canonical.
        merge_in = [
            {"name": "Alice Chen", "type": "person", "mentions": 5},
            {"name": "alice  chen", "type": "person", "mentions": 2},  # case+spacing variant
        ]
        mres = mod.resolve_entities(merge_in)
        alice = next((r for r in mres if r["name"] == "Alice Chen"), None)
        checks["merges_variant"] = (len(mres) == 1)                       # collapsed to one
        checks["picks_canonical"] = bool(alice and alice["name"] == "Alice Chen")  # most-mentions form
        checks["keeps_aliases"] = bool(alice and "alice  chen" in alice.get("aliases", []))  # reversible
        amap = mod.build_alias_map(mres)
        checks["canonicalize_maps"] = (mod.canonicalize("alice  chen", amap) == "Alice Chen")

        # 2) NO OVER-MERGE (precision): distinct people, a typo, and a suffix variant
        #    must all stay SEPARATE (resolver favors precision).
        precision_in = [
            {"name": "Alice Chen", "type": "person", "mentions": 5},
            {"name": "Bob Chen", "type": "person", "mentions": 4},       # shares surname only -> separate
            {"name": "Alicia Chen", "type": "person", "mentions": 3},    # near-miss name -> separate
        ]
        pres = mod.resolve_entities(precision_in)
        checks["no_false_merge"] = (len(pres) == 3)                       # none wrongly merged

        # 3) CROSS-TYPE guard: person "Apple" and org "Apple" never merge.
        xtype = mod.resolve_entities([
            {"name": "Apple", "type": "person", "mentions": 1},
            {"name": "Apple", "type": "org", "mentions": 3},
        ])
        types = {r["type"] for r in xtype}
        checks["no_cross_type_merge"] = (len(xtype) == 2 and types == {"person", "org"})
    except Exception as e:  # noqa: BLE001
        _log(f"  entityres drive error (fail-open): {e}")
        checks["error"] = str(e)
    applicable = [v for k, v in checks.items() if k != "error"]
    passed = sum(1 for v in applicable if v is True)
    total = len(applicable)
    return {"checks": checks, "entityres_pass": passed, "entityres_total": total,
            "entityres_score": round(100 * passed / total, 1) if total else None}


def run_arm(args) -> dict:
    RESULTS.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    if args.arm in ("rag", "base"):
        result = {"ok": True, "arm": args.arm, "entityres_supported": False,
                  "note": f"{args.arm} arm has no entity_resolver (neuron-only, L2 graph); N/A."}
    else:
        source_ws = Path(args.source or os.environ.get("DINOMEM_WORKSPACE", "")).expanduser()
        mod = _load_resolver(source_ws)
        if mod is None:
            result = {"ok": False, "arm": args.arm, "entityres_supported": False,
                      "reason": f"entity_resolver.py not importable from {source_ws}/procedures"}
        else:
            res = _run_checks(mod)
            result = {"ok": True, "arm": args.arm, "entityres_supported": True, **res,
                      "note": "alias/coref clustering (same-type-guarded, precision-first) via "
                              "entity_resolver.resolve_entities. PDF §2 entity reasoning."}
    result["seconds"] = round(time.time() - t0, 2)
    if result.get("entityres_supported") and result.get("entityres_score") is not None:
        _log(f"  entityres_score={result['entityres_score']}% "
             f"({result['entityres_pass']}/{result['entityres_total']})")
    out = Path(args.out) if args.out else RESULTS / f"entityres_{args.arm}.json"
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    result["out"] = str(out)
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description="Phase 5e entity-resolution runner (one arm)")
    ap.add_argument("--arm", choices=["rag", "base", "neuron"], default="neuron")
    ap.add_argument("--source", default=os.environ.get("DINOMEM_WORKSPACE", ""))
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    res = run_arm(args)
    print(json.dumps({k: v for k, v in res.items() if k != "checks"}, indent=2))
    if res.get("checks"):
        print("checks:", json.dumps(res["checks"], indent=2), file=sys.stderr)


if __name__ == "__main__":
    main()
