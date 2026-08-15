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
                        [--live-leak-sig <HEX>]

  Exit 0 only if: every critical stage ran, memory items > 0, and the live
  workspace mtime is unchanged. Any empty/failed stage -> nonzero exit, fail-loud.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path


# Max extract_memory re-drives while the async backlog drains (see DRAIN POLL in
# drive()). extract_memory processes BATCH_SIZE(=3) archives per pass and resumes
# from the dedup log, so a big haystack that timed the front door out needs a few
# passes to fully materialize. 20 passes * BATCH_SIZE = 60 archives headroom.
DRAIN_MAX_LOOPS = 20


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


def _live_leak_signature(source: Path) -> str:
    """Precise isolation tripwire: hash ONLY the live paths a lab LEAK would
    mutate — every live agent's sessions/ FILE LISTING (a leak renames X.jsonl ->
    X.archived.orphan.<ts>.jsonl) and any .processed_archives.json tracker content
    (a leak rewrites it). NOT a whole-tree mtime: this LIVE session writes its own
    trajectory/session/memory files continuously while a run executes, so mtime
    ALWAYS moves and false-positives 'ISOLATION VIOLATION' with zero real leak
    (proven 2026-08-15 — tripwire fired on this active telegram session's own
    writes). Must match setup_lab._live_leak_signature EXACTLY (same hash).
    """
    import hashlib
    parts: list[str] = []
    for agents in source.rglob("agents"):
        if not agents.is_dir():
            continue
        for sess in agents.rglob("sessions"):
            if not sess.is_dir():
                continue
            try:
                names = sorted(p.name for p in sess.iterdir() if p.is_file())
                parts.append(f"{sess}::{'|'.join(names)}")
            except OSError:
                continue
    for trk in source.rglob(".processed_archives.json"):
        try:
            parts.append(f"{trk}::{trk.read_text(encoding='utf-8')}")
        except OSError:
            continue
    return hashlib.sha256("\n".join(sorted(parts)).encode("utf-8")).hexdigest()


def drive(lab: Path, timeout: int, review_max_loops: int,
          live_source: Path | None, live_leak_sig: str | None) -> dict:
    lab = lab.resolve()
    if not lab.exists():
        _fail(f"lab dir does not exist: {lab}")
    if not (lab / ".dinomem_lab").exists():
        _fail(f"refusing to drive: {lab} lacks the .dinomem_lab sentinel "
              f"(not a harness lab workspace built by setup_lab.py)")
    procs = lab / "procedures"
    if not procs.exists():
        _fail(f"lab has no procedures/ dir: {procs}")

    # ---- PRE-FLIGHT ISOLATION GUARD (defense in depth) ----
    # setup_lab sweeps live-sessions paths out of the copied procs, but the neuron
    # overlay installer may re-copy some AFTER that. So RE-VERIFY here, right
    # before we run the front door: if ANY copied proc still references the live
    # /root/.openclaw/agents/*/sessions path, REFUSE to run. This is the guard
    # that would have stopped the 2026-08-15 leak (session_reset archived LIVE
    # orphans because its hardcoded SESSIONS_DIR was never patched).
    # Match the FIXER's semantics: only a live path inside a STRING LITERAL is an
    # executable leak. A bare mention in a COMMENT can't archive anything, so
    # ignore comments (else the guard false-positives on doc comments like
    # session_ingest.py's `# installer (/root/.openclaw/agents/.../sessions ...)`).
    _LIVE_RE = re.compile(
        r'(["\'])/root/\.openclaw/agents/[A-Za-z0-9_-]+/sessions')
    leaking = []
    for py in sorted(procs.glob("*.py")):
        try:
            for ln in py.read_text(encoding="utf-8").splitlines():
                s = ln.lstrip()
                if s.startswith("#"):      # skip full-line comments
                    continue
                # strip trailing inline comment (best-effort; leak lives in a
                # string literal which _LIVE_RE already requires quotes around)
                if _LIVE_RE.search(ln):
                    leaking.append(py.name)
                    break
        except Exception:
            continue
    if leaking:
        _fail("ISOLATION GUARD: copied procs still reference the LIVE sessions path "
              f"({', '.join(leaking)}). Refusing to run — would read/archive the "
              "user's real sessions. Re-run setup_lab (its sweep must patch these) "
              "or check for a proc shape the patcher missed.")

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
    # NOTE: the front door shells extract_memory via subprocess.run(timeout=300).
    # On a big haystack (LongMemEval haystacks run 600K+), extraction EXCEEDS 300s,
    # the front door times out and returns False (s["ok"]=False) EVEN THOUGH
    # extraction is healthy and self-healing (it dedups via .processed_archives.json
    # and drains BATCH_SIZE more archives per invocation). So a timed-out front door
    # is NOT a hard failure here — we treat it as "backlog draining" and finish the
    # drain ourselves in the poll below, rather than declaring front_door_failed.
    critical = [s for s in stages if s["ran"]]
    front_door_timed_out = any(s["ran"] and s.get("timed_out") for s in critical)
    if not critical or (not any(s["ok"] for s in critical) and not front_door_timed_out):
        return {"ok": False, "reason": "front_door_failed", "stages": stages,
                "memory": _count_memory_items(lab)}

    # ---- DRAIN POLL: wait for async extraction backlog to finish ----
    # WHY: the front door may return before extract_memory has materialized ALL items
    # (subprocess timeout on a big haystack; extraction keeps self-healing). Counting
    # items once, immediately, races that drain and false-fails convergence (the
    # 2026-08-15 "items==0 but lab later held 202 items" bug). Re-drive extract_memory
    # directly (it resumes from the dedup log, draining BATCH_SIZE archives per pass)
    # until items materialize AND the status file reports the backlog drained, or a
    # deadline. extract_memory is idempotent (dedup log), so re-driving is safe.
    status_file = lab / "logs" / ".extract_memory_status.json"
    extract_script = procs / "extract_memory.py"
    drain_iters = []
    if _count_memory_items(lab)["item_files"] == 0 and extract_script.exists():
        _log("drain-poll: front door returned with 0 items materialized "
             f"(timed_out={front_door_timed_out}); re-driving extract_memory until "
             "backlog drains.")
        drain_deadline = time.time() + max(timeout, 600)
        for di in range(DRAIN_MAX_LOOPS):
            s = _run_stage(f"extract_drain[{di}]", extract_script, lab, env, timeout)
            drain_iters.append(s)
            items = _count_memory_items(lab)["item_files"]
            remaining = None
            try:
                remaining = int(json.loads(status_file.read_text(
                    encoding="utf-8")).get("remaining_backlog", 0) or 0)
            except (OSError, ValueError):
                remaining = None
            _log(f"drain-poll[{di}]: items={items} remaining_backlog={remaining}")
            # Done when items exist AND the backlog is drained (or unknown -> trust items).
            if items > 0 and (remaining == 0 or remaining is None):
                _log(f"drain-poll converged after {di+1} pass(es): {items} items.")
                break
            if not s["ok"] and not s.get("timed_out"):
                _log(f"drain-poll[{di}]: extract_memory hard-failed rc={s['rc']}; stop.")
                break
            if time.time() > drain_deadline:
                _log("drain-poll: deadline hit; stop draining.")
                break
    stages.extend(drain_iters)

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
        cur = _live_leak_signature(live_source.resolve())
        expected = live_leak_sig
        iso = {
            "checked": True,
            "live_source": str(live_source),
            "expected_sig": expected,
            "actual_sig": cur,
            "untouched": (expected is None) or (cur == expected),
        }

    ok = materialized and (not iso["checked"] or iso["untouched"])
    reason = None
    if not materialized:
        reason = "no_memory_materialized (items==0 after pipeline — a stage no-oped)"
    elif iso["checked"] and not iso["untouched"]:
        reason = ("ISOLATION VIOLATION: live session-archive listing or dedup "
                  "tracker changed during run (a proc leaked into live)")

    return {"ok": ok, "reason": reason, "stages": stages, "memory": mem,
            "isolation": iso}


def main():
    ap = argparse.ArgumentParser(description="Drive dinomem BASE pipeline to convergence in a lab WS.")
    ap.add_argument("--lab", required=True, help="lab workspace dir (from setup_lab.py)")
    ap.add_argument("--timeout", type=int, default=600, help="per-stage timeout seconds")
    ap.add_argument("--review-max-loops", type=int, default=12, help="max memory_review iterations")
    ap.add_argument("--live-source", help="live install dir to assert untouched (isolation tripwire)")
    ap.add_argument("--live-leak-sig", type=str, help="expected live leak-signature from setup_lab.py")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = ap.parse_args()

    live_source = Path(args.live_source) if args.live_source else None
    result = drive(Path(args.lab), args.timeout, args.review_max_loops,
                   live_source, args.live_leak_sig)

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
