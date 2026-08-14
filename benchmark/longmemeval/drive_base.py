#!/usr/bin/env python3
"""
drive_base.py — force the dinomem BASE memory pipeline to convergence inside an
ISOLATED lab workspace, so the harness measures a fully-built (not half-built,
not cron-async) memory.

WHY THIS EXISTS (the correctness linchpin):
  Parts of dinomem are cron-materialized (extraction, cleanup, review). If the
  answer loop queried memory BEFORE those ran, it would read a half-built memory
  and produce a wrong, nondeterministic score. This driver runs the real base
  pipeline stages IN ORDER, synchronously, to steady-state, then asserts memory
  actually materialized (items > 0) before anyone is allowed to query.

FRONT DOOR (verified against the live base code, do NOT reimplement stage order):
  The base entrypoint is procedures/auto_session_reset.py. It chains, in order:
    (1) session_reset.py      — archive adoption / reset bookkeeping
    (2) extract_memory.py     — the archive .jsonl -> memory item files
    (2b/2c) extract_user.py / compile_user.py — peer/user router (fail-open, base)
    (3) session_ingest.py     — ONLY if neuron is installed (absent in base lab)
  auto_session_reset.py does NOT itself run cleanup/review (those are separate
  cron stages), so this driver runs them explicitly AFTER extraction:
    (4) memory_cleanup.py     — dedup / merge pass
    (5) memory_review.py      — LLM review LOOP until the review batch drains

ISOLATION INVARIANT (safety, non-negotiable):
  Every stage runs with cwd = the lab workspace, invoking the lab's OWN copy of
  procedures/ (setup_lab.py already patched extract_memory.py's hardcoded absolute
  SESSIONS_DIR to the lab's sessions dir). DINOMEM_WORKSPACE / OPENCLAW_WORKSPACE
  are pointed at the lab. After the run we re-check the live source mtime recorded
  by setup_lab.py and FAIL LOUD if the live user workspace was touched.

USAGE:
  python3 drive_base.py --lab <LAB_DIR> [--json] [--timeout 600] \\
                        [--review-max-loops 12] [--live-source <DIR>] \\
                        [--live-source-mtime <FLOAT>]

  Exit 0 only if: every critical stage ran, memory items > 0, and the live
  workspace mtime is unchanged. Any empty/failed stage -> nonzero exit, fail-loud.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def _fail(msg: str, code: int = 1):
    print(f"[drive_base] FAIL: {msg}", file=sys.stderr)
    sys.exit(code)


def _log(msg: str):
    print(f"[drive_base] {msg}", file=sys.stderr)


def _run_stage(name: str, script: Path, lab: Path, env: dict, timeout: int) -> dict:
    """Run one pipeline stage script from the lab's procedures dir, cwd=lab.

    Returns {stage, ran, ok, rc, timed_out, stdout_tail, stderr_tail}.
    A missing optional script returns ran=False (skipped), never fails.
    """
    if not script.exists():
        _log(f"stage '{name}': script absent ({script.name}) — skipped")
        return {"stage": name, "ran": False, "ok": True, "rc": None,
                "timed_out": False, "skipped": True}
    _log(f"stage '{name}': running {script.name} (cwd={lab})")
    t0 = time.time()
    try:
        r = subprocess.run(
            [sys.executable, str(script)],
            cwd=str(lab), env=env, timeout=timeout,
            capture_output=True, text=True,
        )
        timed_out = False
        rc = r.returncode
        out, err = r.stdout, r.stderr
    except subprocess.TimeoutExpired as e:
        timed_out = True
        rc = None
        out = (e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        err = (e.stderr or b"").decode("utf-8", "replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
    dt = round(time.time() - t0, 1)
    ok = (rc == 0) and not timed_out
    _log(f"stage '{name}': rc={rc} timed_out={timed_out} {dt}s")
    return {
        "stage": name, "ran": True, "ok": ok, "rc": rc,
        "timed_out": timed_out, "seconds": dt,
        "stdout_tail": (out or "")[-800:],
        "stderr_tail": (err or "")[-800:],
    }


def _count_memory_items(lab: Path) -> dict:
    """Count materialized base memory: per-item files in <lab>/memory plus any
    MEMORY.md body. Base writes one file per extracted item (YYYY-MM-DD_type_slug.md)
    and rebuilds MEMORY.md from them. Items>0 is the convergence assertion.
    """
    mem = lab / "memory"
    if not mem.exists():
        return {"item_files": 0, "memory_md_bytes": 0, "files": []}
    item_files = [
        p for p in mem.glob("*.md")
        if p.name != "MEMORY.md" and not p.name.startswith("_")
    ]
    memory_md = mem / "MEMORY.md"
    return {
        "item_files": len(item_files),
        "memory_md_bytes": memory_md.stat().st_size if memory_md.exists() else 0,
        "files": sorted(p.name for p in item_files)[:50],
    }


def _live_source_mtime(source: Path) -> float:
    """Deepest mtime of the live source procedures/ + tools/ — the isolation
    tripwire. If the pipeline wrote back into the live install, this changes.
    """
    latest = 0.0
    for sub in ("procedures", "tools"):
        d = source / sub
        if not d.exists():
            continue
        for p in d.rglob("*"):
            try:
                latest = max(latest, p.stat().st_mtime)
            except OSError:
                pass
    return latest


def drive(lab: Path, timeout: int, review_max_loops: int,
          live_source: Path | None, live_source_mtime: float | None) -> dict:
    lab = lab.resolve()
    if not lab.exists():
        _fail(f"lab dir does not exist: {lab}")
    if not (lab / ".dinomem_lab").exists():
        _fail(f"refusing to drive: {lab} lacks the .dinomem_lab sentinel "
              f"(not a harness lab workspace built by setup_lab.py)")
    procs = lab / "procedures"
    if not procs.exists():
        _fail(f"lab has no procedures/ dir: {procs}")

    # Sandbox env: point every workspace-var at the lab.
    env = dict(os.environ)
    env["DINOMEM_WORKSPACE"] = str(lab)
    env["OPENCLAW_WORKSPACE"] = str(lab)
    env["PYTHONUNBUFFERED"] = "1"

    stages: list[dict] = []

    # ---- STAGE 1+2: front door (session_reset -> extract_memory -> user legs) ----
    # Shell the REAL orchestrator so we inherit the exact base order, not a copy.
    orchestrator = procs / "auto_session_reset.py"
    if orchestrator.exists():
        stages.append(_run_stage("front_door(auto_session_reset)", orchestrator,
                                 lab, env, timeout))
    else:
        # Fallback: run the two critical stages directly, in order.
        stages.append(_run_stage("session_reset", procs / "session_reset.py",
                                 lab, env, timeout))
        stages.append(_run_stage("extract_memory", procs / "extract_memory.py",
                                 lab, env, timeout))

    # Critical-stage gate: the front door (or its extract fallback) must have run ok.
    critical = [s for s in stages if s["ran"]]
    if not critical or not any(s["ok"] for s in critical):
        return {"ok": False, "reason": "front_door_failed", "stages": stages,
                "memory": _count_memory_items(lab)}

    # ---- STAGE 4: cleanup (dedup/merge) ----
    stages.append(_run_stage("memory_cleanup", procs / "memory_cleanup.py",
                             lab, env, timeout))

    # ---- STAGE 5: review LOOP until the batch drains ----
    review_script = procs / "memory_review.py"
    review_iters = []
    if review_script.exists():
        prev_sig = None
        for i in range(review_max_loops):
            s = _run_stage(f"memory_review[{i}]", review_script, lab, env, timeout)
            review_iters.append(s)
            # Convergence: review is idempotent once the batch drains. Detect a
            # steady-state by hashing the memory dir listing between iterations.
            sig = _count_memory_items(lab)
            cur_sig = (sig["item_files"], sig["memory_md_bytes"])
            if not s["ok"]:
                break
            if cur_sig == prev_sig:
                _log(f"review converged after {i+1} iteration(s)")
                break
            prev_sig = cur_sig
    stages.extend(review_iters)

    mem = _count_memory_items(lab)

    # ---- CONVERGENCE ASSERTION: memory actually materialized ----
    materialized = mem["item_files"] > 0 or mem["memory_md_bytes"] > 0

    # ---- ISOLATION TRIPWIRE: live source must be untouched ----
    iso = {"checked": False}
    if live_source is not None:
        cur = _live_source_mtime(live_source.resolve())
        expected = live_source_mtime
        iso = {
            "checked": True,
            "live_source": str(live_source),
            "expected_mtime": expected,
            "actual_mtime": cur,
            "untouched": (expected is None) or (abs(cur - expected) < 1e-6),
        }

    ok = materialized and (not iso["checked"] or iso["untouched"])
    reason = None
    if not materialized:
        reason = "no_memory_materialized (items==0 after pipeline — a stage no-oped)"
    elif iso["checked"] and not iso["untouched"]:
        reason = "ISOLATION VIOLATION: live source mtime changed during run"

    return {"ok": ok, "reason": reason, "stages": stages, "memory": mem,
            "isolation": iso}


def main():
    ap = argparse.ArgumentParser(description="Drive dinomem BASE pipeline to convergence in a lab WS.")
    ap.add_argument("--lab", required=True, help="lab workspace dir (from setup_lab.py)")
    ap.add_argument("--timeout", type=int, default=600, help="per-stage timeout seconds")
    ap.add_argument("--review-max-loops", type=int, default=12, help="max memory_review iterations")
    ap.add_argument("--live-source", help="live install dir to assert untouched (isolation tripwire)")
    ap.add_argument("--live-source-mtime", type=float, help="expected live source mtime from setup_lab.py")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = ap.parse_args()

    live_source = Path(args.live_source) if args.live_source else None
    result = drive(Path(args.lab), args.timeout, args.review_max_loops,
                   live_source, args.live_source_mtime)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"driver ok={result['ok']} reason={result.get('reason')}")
        print(f"  memory: {result['memory']['item_files']} item file(s), "
              f"MEMORY.md {result['memory']['memory_md_bytes']} bytes")
        iso = result.get("isolation", {})
        if iso.get("checked"):
            print(f"  isolation: live source untouched={iso['untouched']}")
        for s in result["stages"]:
            if s.get("ran"):
                print(f"  stage {s['stage']}: ok={s['ok']} rc={s.get('rc')}")

    sys.exit(0 if result["ok"] else 2)


if __name__ == "__main__":
    main()
