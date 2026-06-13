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
from pathlib import Path
from datetime import datetime

LOG_FILE = Path(__file__).parent.parent / "logs" / "auto_reset.log"
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


def main():
    log("")
    log("=" * 60)
    log("🦴 AUTO SESSION RESET ORCHESTRATOR")
    log(f"⏰ Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log("=" * 60)

    # Step 1: Session reset (critical — must not fail)
    session_ok = run_script("session_reset.py")

    # Step 2: Memory extraction (non-critical — can fail independently)
    memory_ok = run_script("extract_memory.py")

    # Final status
    log("")
    log("=" * 60)
    log("📋 ORCHESTRATOR SUMMARY")
    log(f"   • Session reset: {'✅ OK' if session_ok else '❌ FAILED'}")
    log(f"   • Memory extraction: {'✅ OK' if memory_ok else '⚠️ FAILED'}")
    log("=" * 60)

    # Exit with error only if session reset failed
    sys.exit(0 if session_ok else 1)


if __name__ == "__main__":
    main()
