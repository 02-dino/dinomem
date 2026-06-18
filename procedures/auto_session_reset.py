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

import subprocess
import sys
import os
import fcntl
from pathlib import Path
from datetime import datetime

LOG_FILE = Path(__file__).parent.parent / "logs" / "auto_reset.log"
LOCK_FILE = Path("/tmp/dinomem_auto_reset.lock")
LOG_FILE.parent.mkdir(exist_ok=True)


def log(message):
    """Write to log file with timestamp"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_message = f"[{timestamp}] {message}\n"
    print(log_message.strip())
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(log_message)


def run_script(script_name):
    """Run a subprocess script, return True on success."""
    workspace = Path(__file__).parent.parent
    script_path = workspace / "procedures" / script_name
    log(f"🔄 Running {script_name}...")
    try:
        result = subprocess.run(
            [sys.executable, str(script_path)],
            cwd=str(workspace),
            timeout=300
        )
        if result.returncode == 0:
            log(f"✅ {script_name} completed successfully")
            return True
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

def _run_main():
    # Step 1: Session reset (critical — must not fail)
    session_ok = run_script("session_reset.py")

    # Step 2: Memory extraction (non-critical — can fail independently)
    memory_ok = run_script("extract_memory.py")

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
    log(f"   • Memory extraction: {'✅ OK' if memory_ok else '⚠️ FAILED'}")
    if ingest_ok is not None:
        log(f"   • Session ingest: {'✅ OK' if ingest_ok else '⚠️ FAILED'}")
    log("=" * 60)

    # Exit with error only if session reset failed
    sys.exit(0 if session_ok else 1)


if __name__ == "__main__":
    main()
