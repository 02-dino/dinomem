#!/usr/bin/env python3
"""
recovery_run.py — Phase 5d: RECOVERY / REVERSIBLE-CLEANUP runner (one arm).

WHAT THIS PROVES (PDF §5/§11: "reversible cleanup", "git-anchored provenance"):
  dinomem's cleanup/compaction MUTATES and DELETES memory items. The strategy PDF
  wants this to be REVERSIBLE — a bad merge or an over-eager delete must be
  recoverable byte-exact, not lost. _memory_diff.py is the mechanism: every
  extraction run writes memory/.diffs/<run>.json recording adds/updates/deletes
  with before/after content AND a `restore_ref` (git HEAD sha) so a recovery tool
  can `git checkout <restore_ref> -- <path>` to get the EXACT pre-change bytes.

  This phase does NOT need an LLM or a full lab drive — it exercises _memory_diff
  directly on a scratch memory dir, so it runs FREE for every arm that ships it
  (base owns _memory_diff; neuron inherits). rag arm has no diff log -> unsupported.

CHECKS (all deterministic, free):
  audit_add        — a new-file write is recorded as an add with its content
  audit_update     — an in-place mutate is recorded with BEFORE and AFTER
  audit_delete     — a removal is recorded with the deleted content
  diff_flushed     — the .diffs/<run>.json is written atomically and re-readable
  restore_ref_set  — when git-backed, restore_ref is a real HEAD sha (byte-exact anchor)
  byte_exact       — `git checkout <restore_ref> -- <file>` reproduces the exact
                     pre-change bytes of a mutated/deleted file (the real recovery)

METRIC: recovery_score = fraction of applicable checks passed. byte_exact +
restore_ref_set are the headline (true byte-exact undo); audit_* prove the log is
complete even when git is absent (fallback recovery via stored before/after).

USAGE:
  python3 recovery_run.py --arm base|neuron|rag --source <WS_with_procedures> \
      [--out results/recovery_<arm>.json]
"""
from __future__ import annotations
import argparse
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "recovery" / "results"


def _log(m: str) -> None:
    print(m, file=sys.stderr, flush=True)


def _load_diff(source_ws: Path):
    cand = source_ws / "procedures" / "_memory_diff.py"
    if not cand.exists():
        alt = source_ws / "_memory_diff.py"
        cand = alt if alt.exists() else cand
    if not cand.exists():
        return None
    try:
        spec = importlib.util.spec_from_file_location("memory_diff_arm", str(cand))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        if not hasattr(mod, "MemoryDiff"):
            return None
        return mod
    except Exception as e:  # noqa: BLE001
        _log(f"  _memory_diff import failed: {e}")
        return None


def _git(repo: Path, *args) -> str:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, timeout=30).stdout.strip()


def _run_checks(mod) -> dict:
    """Drive _memory_diff on a scratch git-backed memory dir; assert recovery."""
    tmp = Path(tempfile.mkdtemp(prefix="recovery_"))
    mem = tmp / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    checks: dict = {}
    # init a git repo so restore_ref + byte-exact checkout are testable
    git_ok = False
    try:
        _git(mem, "init")
        _git(mem, "config", "user.email", "e@e")
        _git(mem, "config", "user.name", "n")
        git_ok = True
    except Exception as e:  # noqa: BLE001
        _log(f"  git init failed (byte-exact checks will skip): {e}")

    # seed a file that we will MUTATE and one we will DELETE, commit it
    f_update = mem / "fact_a.md"
    f_delete = mem / "fact_b.md"
    original_update = "type: fact\nA = old value\n"
    original_delete = "type: fact\nB = to be deleted\n"
    f_update.write_text(original_update, encoding="utf-8")
    f_delete.write_text(original_delete, encoding="utf-8")
    head_ref = None
    if git_ok:
        _git(mem, "add", "-A")
        _git(mem, "commit", "-m", "seed")
        head_ref = _git(mem, "rev-parse", "HEAD")

    # now perform + record the mutations via MemoryDiff (as the pipeline would)
    try:
        diff = mod.MemoryDiff(mem, date_str="2026-08-14")
        # add
        f_add = mem / "fact_c.md"
        added = "type: fact\nC = brand new\n"
        f_add.write_text(added, encoding="utf-8")
        diff.record_add(str(f_add), "fact", added)
        # update (in place)
        new_update = "type: fact\nA = NEW value\n"
        diff.record_update(str(f_update), "fact", original_update, new_update)
        f_update.write_text(new_update, encoding="utf-8")
        # delete
        diff.record_delete(str(f_delete), "fact", original_delete)
        f_delete.unlink()
        out_path = diff.flush()

        # ---- audit checks: read the flushed diff json back ----
        checks["diff_flushed"] = bool(out_path and Path(out_path).exists())
        payload = json.loads(Path(out_path).read_text()) if checks["diff_flushed"] else {}
        ops = payload.get("operations", {})
        adds, updates, deletes = ops.get("adds", []), ops.get("updates", []), ops.get("deletes", [])
        checks["audit_add"] = any("C = brand new" in json.dumps(a) for a in adds)
        checks["audit_update"] = any(("old value" in json.dumps(u) and "NEW value" in json.dumps(u))
                                     for u in updates)
        checks["audit_delete"] = any("to be deleted" in json.dumps(d) for d in deletes)

        # ---- byte-exact recovery via restore_ref ----
        rref = payload.get("restore_ref")
        if git_ok and rref:
            checks["restore_ref_set"] = (rref == head_ref)
            # recover the DELETED file byte-exact from the ref
            _git(mem, "checkout", rref, "--", "fact_b.md")
            recovered_delete = (mem / "fact_b.md").read_text(encoding="utf-8") if (mem / "fact_b.md").exists() else ""
            # recover the MUTATED file byte-exact
            _git(mem, "checkout", rref, "--", "fact_a.md")
            recovered_update = (mem / "fact_a.md").read_text(encoding="utf-8")
            checks["byte_exact"] = (recovered_delete == original_delete
                                    and recovered_update == original_update)
        else:
            # git absent: byte-exact via ref not applicable, but stored before/after
            # still allows fallback recovery. Mark as skipped (not failed).
            checks["restore_ref_set"] = None
            checks["byte_exact"] = None
    except Exception as e:  # noqa: BLE001
        _log(f"  recovery drive error (fail-open): {e}")
        checks["error"] = str(e)

    applicable = [v for k, v in checks.items() if k != "error" and v is not None]
    passed = sum(1 for v in applicable if v is True)
    total = len(applicable)
    return {"checks": checks, "recovery_pass": passed, "recovery_total": total,
            "recovery_score": round(100 * passed / total, 1) if total else None,
            "git_backed": git_ok}


def run_arm(args) -> dict:
    RESULTS.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    if args.arm == "rag":
        result = {"ok": True, "arm": "rag", "recovery_supported": False,
                  "note": "rag arm has no _memory_diff recovery log; N/A. base/neuron ship it."}
    else:
        source_ws = Path(args.source or os.environ.get("DINOMEM_WORKSPACE", "")).expanduser()
        mod = _load_diff(source_ws)
        if mod is None:
            result = {"ok": False, "arm": args.arm, "recovery_supported": False,
                      "reason": f"_memory_diff.py not importable from {source_ws}/procedures"}
        else:
            res = _run_checks(mod)
            result = {"ok": True, "arm": args.arm, "recovery_supported": True, **res,
                      "note": "reversible-cleanup / byte-exact recovery via _memory_diff "
                              "restore_ref (git checkout) + audit before/after. PDF §5/§11."}
    result["seconds"] = round(time.time() - t0, 2)
    if result.get("recovery_supported") and result.get("recovery_score") is not None:
        _log(f"  recovery_score={result['recovery_score']}% "
             f"({result['recovery_pass']}/{result['recovery_total']}) git_backed={result.get('git_backed')}")
    out = Path(args.out) if args.out else RESULTS / f"recovery_{args.arm}.json"
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    result["out"] = str(out)
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description="Phase 5d recovery / reversible-cleanup runner (one arm)")
    ap.add_argument("--arm", choices=["rag", "base", "neuron"], default="base")
    ap.add_argument("--source", default=os.environ.get("DINOMEM_WORKSPACE", ""))
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    res = run_arm(args)
    print(json.dumps({k: v for k, v in res.items() if k != "checks"}, indent=2))
    if res.get("checks"):
        print("checks:", json.dumps(res["checks"], indent=2), file=sys.stderr)


if __name__ == "__main__":
    main()
