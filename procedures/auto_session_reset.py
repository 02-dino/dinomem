#!/usr/bin/env python3
"""
Auto Session Reset Orchestrator

Runs session reset then memory extraction sequentially.
Failure in memory extraction does NOT affect session reset.

Usage:
  python3 procedures/auto_session_reset.py

Cron (unchanged from original):
  */15 * * * * cd DINOMEM_WORKSPACE_PLACEHOLDER && python3 procedures/auto_session_reset.py >> DINOMEM_WORKSPACE_PLACEHOLDER/logs/auto_reset.log 2>&1

Logs:
  - Orchestrator: logs/auto_reset.log (high-level status)
  - Session reset: logs/session_reset.log (detailed)
  - Memory extraction: logs/extract_memory.log (detailed)
"""

import shutil
import subprocess
import sys
import os
import fcntl
import json
from pathlib import Path
from datetime import datetime

LOG_FILE = Path(__file__).parent.parent / "logs" / "auto_reset.log"
# PER-AGENT LOCK (was a single global /tmp/dinomem_auto_reset.lock shared by ALL
# agents on a multi-agent box -> whenever two agents' cron ticks overlapped by a
# minute, the later one lost the race and quiet-skipped with 'Another instance is
# running', so busy agents could go hours without ever resetting. Key the lock on
# this agent's own workspace dir name so each agent has its own lane.)
_WS_NAME = Path(__file__).parent.parent.name or "default"
LOCK_FILE = Path(f"/tmp/dinomem_auto_reset_{_WS_NAME}.lock")
EXTRACT_STATUS_FILE = Path(__file__).parent.parent / "logs" / ".extract_memory_status.json"
LOG_FILE.parent.mkdir(exist_ok=True)


def log(message):
    """Write to log file with timestamp"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_message = f"[{timestamp}] {message}\n"
    print(log_message.strip())
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(log_message)


# Distinct exit code a stage may use to say "I intentionally SKIPPED (a concurrent
# instance holds my lock)" — neither success nor failure. extract_memory.py exits
# 3 on a lock-skip so we don't log a misleading "✅ completed" when nothing ran.
SKIP_EXIT_CODE = 3

# ── Cross-agent serialization for embedding-heavy stages ─────────────────────
# WHY (real incident 2026-10-04): every workspace runs this orchestrator on its
# OWN cron lane — 11 lanes on the box where this was found. session_ingest.py
# embeds through the shared local TEI container, and TEI's
# --max-concurrent-requests is a QUEUE DEPTH: past it callers get HTTP 429, not
# a queue slot. 11 lanes against a depth of 3 made interactive memory_search
# return 429 and surface to the user as "memory_search timed out", while TEI
# logged `no permits available`. Raising the depth alone only moves the cliff.
#
# Fix: take the SAME box-wide `heavy-embed` flock that scripts/dinomem_run.sh
# already uses for its heavy classes, so at most one agent embeds at a time no
# matter how many lanes fire.
#
# `--conflict-exit-code 3` is deliberate: it maps onto SKIP_EXIT_CODE above, so
# a lane that loses the race logs an honest "SKIPPED" and self-heals next tick
# instead of being misreported as a failure. The wrapped script's own exit code
# passes through untouched (verified on util-linux 2.42: a wrapped `exit 7`
# still returns 7, and a held lock returns 3).
#
# FAIL-OPEN by design: no flock binary, or no writable lock dir, and the stage
# runs unserialized exactly as before. A lock must never become an outage.
HEAVY_EMBED_STAGES = {"session_ingest.py"}
LOCK_WAIT_SECS = int(os.environ.get("DINOMEM_EMBED_LOCK_WAIT_SECS", "600"))


def _embed_lock_dir():
    """Prefer the tmpfs dir dinomem_run.sh uses; fall back to a persistent one."""
    for cand in (Path("/run/dinomem-locks"), Path.home() / ".dinomem" / "locks"):
        try:
            cand.mkdir(parents=True, exist_ok=True)
            if os.access(cand, os.W_OK):
                return cand
        except Exception:
            continue
    return None


def _wrap_heavy_embed(cmd, script_name):
    """Prefix cmd with flock on the shared heavy-embed lock when possible.

    Returns (cmd, serialized). Any setup failure returns the command unchanged —
    never block a stage because the lock could not be taken.
    """
    if script_name not in HEAVY_EMBED_STAGES:
        return cmd, False
    if not shutil.which("flock"):
        log(f"⚠️  flock not found — running {script_name} unserialized "
            f"(install util-linux to serialize embed load across agents)")
        return cmd, False
    lock_dir = _embed_lock_dir()
    if lock_dir is None:
        log(f"⚠️  no writable lock dir — running {script_name} unserialized")
        return cmd, False
    return ([
        "flock",
        "--timeout", str(LOCK_WAIT_SECS),
        "--conflict-exit-code", str(SKIP_EXIT_CODE),
        str(lock_dir / "heavy-embed.lock"),
    ] + cmd, True)


def run_script(script_name):
    """Run a subprocess script. Returns True on success, False on failure, and the
    string 'skipped' when the stage exited with SKIP_EXIT_CODE (lock held by a
    concurrent instance — not a real failure, but NOT a real success either)."""
    workspace = Path(__file__).parent.parent
    script_path = workspace / "procedures" / script_name
    cmd, serialized = _wrap_heavy_embed(
        [sys.executable, str(script_path)], script_name
    )
    log(f"🔄 Running {script_name}..."
        + (" (serialized on heavy-embed lock)" if serialized else ""))
    try:
        result = subprocess.run(
            cmd,
            cwd=str(workspace),
            # 600s covers the WORK. When serialized, add the bounded flock wait
            # so queueing behind a peer cannot eat the work budget and look like
            # a hang; flock itself gives up at LOCK_WAIT_SECS and exits 3.
            timeout=600 + (LOCK_WAIT_SECS if serialized else 0)
                         # widened 300->600 2026-09-28: session_ingest.py was flaking
                         # against the local TEI embed endpoint on a growing chroma
                         # DB, getting cut off mid-prune every time (fleet-wide fix
                         # across all workspace-* deployments on this box)
        )
        if result.returncode == 0:
            log(f"✅ {script_name} completed successfully")
            return True
        elif result.returncode == SKIP_EXIT_CODE:
            log(f"⏭️  {script_name} SKIPPED (a concurrent instance holds its lock) — "
                f"not a failure, but nothing ran this tick; it self-heals next tick")
            return "skipped"
        else:
            log(f"❌ {script_name} failed (exit code {result.returncode})")
            return False
    except subprocess.TimeoutExpired:
        log(f"⏰ {script_name} timed out after 300s")
        return False
    except Exception as e:
        log(f"❌ {script_name} error: {e}")
        return False


def acquire_lock():
    """Acquire exclusive lock. Returns lock file handle or None if already running."""
    lock_fh = open(LOCK_FILE, 'w')
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_fh.write(str(os.getpid()))
        lock_fh.flush()
        return lock_fh
    except BlockingIOError:
        lock_fh.close()
        try:
            pid = LOCK_FILE.read_text().strip()
            log(f"⏭️  Another instance is running (PID {pid}), skipping")
        except Exception:
            log("⏭️  Another instance is running, skipping")
        return None

def release_lock(lock_fh):
    """Release lock and remove lock file."""
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()
        LOCK_FILE.unlink(missing_ok=True)
    except Exception:
        pass

def main():
    log("")
    log("=" * 60)
    log("🦴 AUTO SESSION RESET ORCHESTRATOR")
    log(f"⏰ Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log("=" * 60)

    lock_fh = acquire_lock()
    if lock_fh is None:
        sys.exit(0)

    try:
        _run_main()
    finally:
        release_lock(lock_fh)

def _memory_extraction_status_line(memory_ok):
    """Distinguish a real extraction failure from a backlog that hit the 300s
    subprocess timeout but is still self-healing (dedups via .processed_archives.json,
    clears more archives every 15-min tick). Reads the small status file
    extract_memory.py writes after every run. Falls back to the old blanket
    FAILED wording if the status file is missing/unreadable (e.g. an older
    extract_memory.py from before this fix, or the subprocess died mid-write)."""
    if memory_ok == "skipped":
        return ("⏭️  SKIPPED (a concurrent extract held the lock) — nothing ran this "
                "tick; self-heals next tick. NOT a failure, but NOT a completed run.")
    if memory_ok:
        return "✅ OK"
    try:
        if EXTRACT_STATUS_FILE.exists():
            status = json.loads(EXTRACT_STATUS_FILE.read_text(encoding="utf-8"))
            remaining = int(status.get("remaining_backlog", 0) or 0)
            note = status.get("note", "")
            if note == "backlog_draining" or (remaining > 0 and note != "real_failure"):
                return f"⏳ IN PROGRESS ({remaining} archive(s) remaining, self-healing — not a real failure)"
    except Exception:
        pass
    return "⚠️ FAILED"


def _run_main():
    # Step 1: Session reset (critical — must not fail)
    session_ok = run_script("session_reset.py")

    # Step 2: Memory extraction (non-critical — can fail independently)
    memory_ok = run_script("extract_memory.py")

    # Step 2b: Peer/user derivation (non-critical, fail-open — second head on the
    # same archive scan). Only runs if the deriver is installed (BASE peer-rep
    # feature). extract_user.py NEVER breaks the pipeline: any error -> it exits 0.
    user_ok = None
    user_script = Path(__file__).parent / "extract_user.py"
    if user_script.exists():
        user_ok = run_script("extract_user.py")

    # Step 2c: Compile the USER.md router (owner block + user map) from the peer
    # reps extract_user just refreshed. Marker-bounded + fail-open: it only
    # rewrites its managed block, never hand-written USER.md content, and any
    # error exits 0 (never breaks the pipeline). Only runs if installed.
    compile_ok = None
    compile_script = Path(__file__).parent / "compile_user.py"
    if compile_script.exists():
        compile_ok = run_script("compile_user.py")

    # Step 3: Session ingest (optional — only if neuron is installed)
    ingest_script = Path(__file__).parent / "session_ingest.py"
    ingest_ok = None
    if ingest_script.exists():
        ingest_ok = run_script("session_ingest.py")

    # Final status
    log("")
    log("=" * 60)
    log("📋 ORCHESTRATOR SUMMARY")
    log(f"   • Session reset: {'✅ OK' if session_ok else '❌ FAILED'}")
    log(f"   • Memory extraction: {_memory_extraction_status_line(memory_ok)}")
    if user_ok is not None:
        log(f"   • Peer derivation: {'✅ OK' if user_ok else '⚠️ FAILED (fail-open, non-critical)'}")
    if ingest_ok is not None:
        log(f"   • Session ingest: {'✅ OK' if ingest_ok else '⚠️ FAILED'}")
    log("=" * 60)

    # Exit with error only if session reset failed
    sys.exit(0 if session_ok else 1)


if __name__ == "__main__":
    main()
