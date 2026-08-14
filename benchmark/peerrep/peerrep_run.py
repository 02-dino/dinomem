#!/usr/bin/env python3
"""
peerrep_run.py — Phase 6: PEER-REPRESENTATION runner (one arm).

Grades extract_user.py (the peer-profile deriver) that the eval DROVE but never
SCORED. Two lanes:

  LANE A — STRUCTURAL (FREE, default): import extract_user from the arm's
    procedures/, point its PEERS_DIR at a scratch dir, call apply_derive() on a
    known derive-JSON, and assert the on-disk rep mechanics:
      supersede_in_place, provenance_logged, history_kept, dedup_ok,
      directive_demoted (authority gate). Pure-python, no LLM -> runs for
      base+neuron at $0. This is the default so the phase is free-tier.

  LANE B — QUALITY (PAID, --quality): drive the real Stage-0/Stage-1 derive on a
    scripted transcript, judge recall of seeded person-facts + rejection of
    distractor world-facts. Needs a model. (Scaffolded; enabled via run_all paid.)

rag arm has no extract_user -> peerrep_supported=False.

WHY apply_derive is safe to call directly: it's fail-open pure-python (regex +
file I/O), no network. We isolate side effects by overriding PEERS_DIR to a temp
dir and (for the non-owner gate) forcing is_owner()->False via env-free synthetic
id that is not in any owner source.

USAGE:
  python3 peerrep_run.py --arm base|neuron|rag --cases derive_cases.json \
      --source <WS_with_procedures> [--quality] [--out results/peerrep_<arm>.json]
"""
from __future__ import annotations
import argparse
import importlib.util
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "peerrep" / "results"


def _log(m: str) -> None:
    print(m, file=sys.stderr, flush=True)


def _load_extract_user(source_ws: Path):
    cand = source_ws / "procedures" / "extract_user.py"
    if not cand.exists():
        alt = source_ws / "extract_user.py"
        cand = alt if alt.exists() else cand
    if not cand.exists():
        return None
    try:
        spec = importlib.util.spec_from_file_location("extract_user_arm", str(cand))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        for fn in ("apply_derive", "ensure_stub", "peer_path"):
            if not hasattr(mod, fn):
                return None
        return mod
    except Exception as e:  # noqa: BLE001
        _log(f"  extract_user import failed: {e}")
        return None


def _structural(mod, s) -> dict:
    """Lane A: call apply_derive on known derive-JSON, assert on-disk mechanics."""
    platform, pid = s["platform"], s["platform_id"]
    # isolate side effects: point PEERS_DIR at a scratch dir
    tmp = Path(tempfile.mkdtemp(prefix="peerrep_"))
    peers = tmp / "peers"
    peers.mkdir(parents=True, exist_ok=True)
    try:
        mod.PEERS_DIR = peers  # override module-global
    except Exception:
        pass
    checks = {}
    try:
        # first derive
        mod.ensure_stub(platform, pid)
        mod.apply_derive(platform, pid, s["derive_1"])
        rep1 = mod.peer_path(platform, pid).read_text(encoding="utf-8")
        a1 = s["assert"]["after_1"]
        checks["after1_contains"] = all(x in rep1 for x in a1["contains"])
        checks["directive_demoted"] = (a1["demoted_marker"] in rep1)
        # the raw directive must NOT be stored as a plain fact line
        checks["directive_not_stored_as_rule"] = (a1["not_contains_rule"] not in rep1)

        # second derive (supersede + dup)
        mod.apply_derive(platform, pid, s["derive_2"])
        rep2 = mod.peer_path(platform, pid).read_text(encoding="utf-8")
        a2 = s["assert"]["after_2"]
        checks["supersede_marker"] = (a2["superseded_marker"] in rep2)
        checks["after2_contains_new"] = all(x in rep2 for x in a2["contains"])
        checks["history_kept"] = (a2["history_kept"] in rep2)   # old value preserved in provenance
        # dedup: the re-asserted fact appears but only ONCE as a "- <fact>  (conf" line
        dup = a2["no_dup_of"]
        occ = rep2.count(f"- {dup}  (conf")
        checks["dedup_ok"] = (occ <= 1)
        rep_final = rep2
    except Exception as e:  # noqa: BLE001
        _log(f"  structural lane error (fail-open): {e}")
        checks["error"] = str(e)
        rep_final = ""
    passed = sum(1 for k, v in checks.items() if k != "error" and v is True)
    total = sum(1 for k in checks if k != "error")
    return {"checks": checks, "structural_pass": passed, "structural_total": total,
            "structural_score": round(100 * passed / total, 1) if total else None,
            "rep_chars": len(rep_final)}


def run_arm(args) -> dict:
    RESULTS.mkdir(parents=True, exist_ok=True)
    spec = json.loads(Path(args.cases).read_text())
    t0 = time.time()

    if args.arm == "rag":
        result = {"ok": True, "arm": "rag", "schema": spec["schema"],
                  "peerrep_supported": False,
                  "note": "rag arm has no extract_user (peer-rep deriver); N/A. "
                          "base/neuron ship it."}
    else:
        source_ws = Path(args.source or os.environ.get("DINOMEM_WORKSPACE", "")).expanduser()
        mod = _load_extract_user(source_ws)
        if mod is None:
            result = {"ok": False, "arm": args.arm, "schema": spec["schema"],
                      "peerrep_supported": False,
                      "reason": f"extract_user.py not importable from {source_ws}/procedures"}
        else:
            struct = _structural(mod, spec["structural"])
            result = {"ok": True, "arm": args.arm, "schema": spec["schema"],
                      "peerrep_supported": True, "lane": "structural",
                      **struct,
                      "note": "Lane A structural (free): supersede-in-place + provenance + "
                              "dedup + authority-demote mechanics of extract_user.apply_derive."}
            if args.quality:
                # Lane B scaffold: paid derive-quality is wired via run_all paid path.
                result["quality_note"] = ("Lane B (paid derive-quality) not run in free mode; "
                                          "enable via run_all.py paid path with a model.")
    result["seconds"] = round(time.time() - t0, 2)
    if result.get("peerrep_supported") and result.get("structural_score") is not None:
        _log(f"  structural_score={result['structural_score']}% "
             f"({result['structural_pass']}/{result['structural_total']} checks)")
    out = Path(args.out) if args.out else RESULTS / f"peerrep_{args.arm}.json"
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    result["out"] = str(out)
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description="Phase 6 peer-representation runner (one arm)")
    ap.add_argument("--arm", choices=["rag", "base", "neuron"], default="neuron")
    ap.add_argument("--cases", required=True)
    ap.add_argument("--source", default=os.environ.get("DINOMEM_WORKSPACE", ""))
    ap.add_argument("--quality", action="store_true", help="also run Lane B derive-quality (paid)")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    res = run_arm(args)
    print(json.dumps({k: v for k, v in res.items() if k != "checks"}, indent=2))


if __name__ == "__main__":
    main()
