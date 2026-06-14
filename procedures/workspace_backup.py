#!/usr/bin/env python3
"""
workspace_backup.py — Weekly snapshot backup + restore for dinomem workspace.

Backs up only irreplaceable files (memory, config, root files).
Skips regenerable data (vector DBs, session archives, logs, caches).
Keeps last 3 snapshots — auto-rotates oldest.

Usage:
  python3 procedures/workspace_backup.py                                          # create snapshot
  python3 procedures/workspace_backup.py --list                                   # list snapshots + cron backups
  python3 procedures/workspace_backup.py --restore                                # restore latest (interactive)
  python3 procedures/workspace_backup.py --restore latest                         # restore latest (no prompt)
  python3 procedures/workspace_backup.py --restore <file>                         # restore specific snapshot
  python3 procedures/workspace_backup.py --restore --file memory/2026-06-01.md   # restore one file
  python3 procedures/workspace_backup.py --restore --file exports/crontab.txt    # restore linux crontab
  python3 procedures/workspace_backup.py --restore --file exports/openclaw-config.json  # restore openclaw config
  python3 procedures/workspace_backup.py --restore-crons                          # restore all openclaw cron jobs
  python3 procedures/workspace_backup.py --restore-crons --agent analyst          # restore analyst crons only

What's inside the tar.gz:
  - memory/, MEMORY.md, AGENTS.md, SOUL.md, IDENTITY.md, TOOLS.md, USER.md, HEARTBEAT.md
  - topics/, docs/
  - openclaw.json
  - exports/crontab.txt        (linux crontab snapshot)
  - exports/openclaw-config.json (openclaw config snapshot)

Separate (not in tar.gz, restored via --restore-crons):
  - .backups/snapshots/openclaw-cron-jobs-{ts}.json

Cron: weekly Sunday 2:00 UTC (registered by install.sh)
"""

import argparse
import json
import os
import subprocess
import sys
import tarfile
from datetime import datetime
from pathlib import Path

WORKSPACE = Path(__file__).parent.parent
BACKUP_DIR = WORKSPACE / ".backups" / "snapshots"
KEEP = 3

INCLUDE = [
    "memory",
    "MEMORY.md",
    "AGENTS.md",
    "SOUL.md",
    "IDENTITY.md",
    "TOOLS.md",
    "USER.md",
    "HEARTBEAT.md",
    "topics",
    "docs",
]

OPENCLAW_JSON = WORKSPACE.parent / "openclaw.json"

SKIP_PATTERNS = [
    "kb/vector_db",
    "kb/vector_db_docs",
    "sessions",
    "logs",
    "__pycache__",
    ".backups",
    ".git",
]

def log(msg): print(f"[workspace_backup] {msg}")

def _snapshots():
    return sorted(BACKUP_DIR.glob("snapshot-*.tar.gz"), reverse=True)

# ── Backup ────────────────────────────────────────────────────────────────────
def create_snapshot():
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    archive = BACKUP_DIR / f"snapshot-{ts}.tar.gz"

    # Export live state into exports/ so they land inside the tar.gz
    _export_linux_crontab()
    _export_openclaw_config()

    with tarfile.open(archive, "w:gz") as tar:
        for item in INCLUDE:
            path = WORKSPACE / item
            if path.exists():
                tar.add(path, arcname=item)
                log(f"  + {item}")
        # exports/ — live state snapshots
        exports_path = WORKSPACE / "exports"
        if exports_path.exists():
            tar.add(exports_path, arcname="exports")
            log(f"  + exports/")
        if OPENCLAW_JSON.exists():
            tar.add(OPENCLAW_JSON, arcname="openclaw.json")
            log(f"  + openclaw.json")

    size_kb = archive.stat().st_size // 1024
    log(f"snapshot: {archive.name} ({size_kb} KB)")

    # Rotate
    for old in _snapshots()[KEEP:]:
        old.unlink()
        log(f"pruned: {old.name}")

    # Backup OpenClaw cron jobs (separate — needs CLI to restore)
    _backup_crons(ts)

    return archive

# ── Live State Exports (go inside tar.gz) ────────────────────────────────────
def _export_linux_crontab():
    exports = WORKSPACE / "exports"
    exports.mkdir(exist_ok=True)
    out = exports / "crontab.txt"
    try:
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=10)
        out.write_text(r.stdout if r.returncode == 0 else "# no crontab\n")
        log(f"exported: exports/crontab.txt")
    except Exception as e:
        log(f"crontab export: skipped ({e})")

def _export_openclaw_config():
    exports = WORKSPACE / "exports"
    exports.mkdir(exist_ok=True)
    out = exports / "openclaw-config.json"
    try:
        r = subprocess.run(["openclaw", "config", "get", "--json"],
                           capture_output=True, text=True, timeout=15)
        if r.returncode == 0 and r.stdout.strip():
            out.write_text(r.stdout)
        elif OPENCLAW_JSON.exists():
            out.write_text(OPENCLAW_JSON.read_text())
        log(f"exported: exports/openclaw-config.json")
    except Exception as e:
        log(f"openclaw config export: skipped ({e})")

# ── Cron Backup ───────────────────────────────────────────────────────────────
CRON_BACKUP_DIR = BACKUP_DIR  # same folder as snapshots

def _backup_crons(ts: str):
    out = CRON_BACKUP_DIR / f"openclaw-cron-jobs-{ts}.json"
    try:
        r = subprocess.run(["openclaw", "cron", "list", "--json"],
                           capture_output=True, text=True, timeout=30)
        if r.returncode == 0 and r.stdout.strip():
            out.write_text(r.stdout)
            log(f"cron backup: {out.name}")
        else:
            log("cron backup: skipped (CLI unavailable)")
    except Exception as e:
        log(f"cron backup: skipped ({e})")
    # Rotate: keep last 5
    for old in sorted(CRON_BACKUP_DIR.glob("openclaw-cron-jobs-*.json"), reverse=True)[5:]:
        old.unlink()
        log(f"cron backup pruned: {old.name}")

def _cron_backups():
    return sorted(CRON_BACKUP_DIR.glob("openclaw-cron-jobs-*.json"), reverse=True)

def list_cron_backups():
    files = _cron_backups()
    if not files:
        log("no cron backups found"); return
    log(f"{len(files)} cron backup(s):")
    for i, f in enumerate(files):
        tag = " (latest)" if i == 0 else ""
        log(f"  [{i+1}] {f.name}{tag}")

def _pick_cron_backup(target: str) -> Path:
    files = _cron_backups()
    if not files:
        log("no cron backups found"); sys.exit(1)
    if not target or target == "latest":
        return files[0]
    for f in files:
        if target in f.name:
            return f
    try:
        return files[int(target) - 1]
    except (ValueError, IndexError):
        log(f"cron backup not found: {target}"); sys.exit(1)

def restore_crons(target: str, agent: str = None, yes: bool = False):
    snap = _pick_cron_backup(target)
    data = json.loads(snap.read_text())
    jobs = data.get("jobs", [])
    if agent:
        jobs = [j for j in jobs if j.get("agentId") == agent]
        log(f"restore {len(jobs)} cron job(s) for agent '{agent}' from: {snap.name}")
    else:
        log(f"restore ALL {len(jobs)} cron job(s) from: {snap.name}")
    if not jobs:
        log("no matching jobs found."); return
    for j in jobs:
        log(f"  - [{j.get('agentId','?')}] {j.get('name')}")
    if not yes:
        confirm = input("  This will ADD these jobs (existing jobs not deleted). Type 'yes' to confirm: ")
        if confirm.strip().lower() != "yes":
            log("cancelled"); return
    ok = fail = 0
    for j in jobs:
        sched = j.get("schedule", {})
        payload = j.get("payload", {})
        delivery = j.get("delivery", {})
        cmd = ["openclaw", "cron", "add", j.get("name", "restored-job")]
        if sched.get("kind") == "cron":
            cmd += ["--cron", sched["expr"]]
            if sched.get("tz"): cmd += ["--tz", sched["tz"]]
        elif sched.get("kind") == "every":
            cmd += ["--every", str(sched["everyMs"]) + "ms"]
        elif sched.get("kind") == "at":
            cmd += ["--at", sched["at"]]
        if j.get("agentId"): cmd += ["--agent", j["agentId"]]
        if j.get("sessionTarget"): cmd += ["--session-target", j["sessionTarget"]]
        if j.get("wakeMode"): cmd += ["--wake-mode", j["wakeMode"]]
        if payload.get("message"): cmd += ["--message", payload["message"]]
        if payload.get("model"): cmd += ["--model", payload["model"]]
        if payload.get("timeoutSeconds"): cmd += ["--timeout", str(payload["timeoutSeconds"])]
        if delivery.get("mode") == "none": cmd += ["--no-announce"]
        elif delivery.get("mode") == "announce": cmd += ["--announce"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if r.returncode == 0:
            log(f"  ✅ {j.get('name')}"); ok += 1
        else:
            log(f"  ❌ {j.get('name')}: {r.stderr.strip() or r.stdout.strip()}"); fail += 1
    log(f"done. {ok} restored, {fail} failed.")

# ── List ──────────────────────────────────────────────────────────────────────
def list_snapshots():
    snaps = _snapshots()
    if not snaps:
        log("no snapshots found")
        return
    log(f"{len(snaps)} snapshot(s) in {BACKUP_DIR}:")
    for i, s in enumerate(snaps):
        tag = " (latest)" if i == 0 else ""
        log(f"  [{i+1}] {s.name} — {s.stat().st_size // 1024} KB{tag}")

# ── Restore ───────────────────────────────────────────────────────────────────
def _pick_snapshot(target: str) -> Path:
    snaps = _snapshots()
    if not snaps:
        log("no snapshots found"); sys.exit(1)
    if not target or target == "latest":
        return snaps[0]
    # Match by filename or index
    for s in snaps:
        if target in s.name:
            return s
    try:
        idx = int(target) - 1
        return snaps[idx]
    except (ValueError, IndexError):
        log(f"snapshot not found: {target}"); sys.exit(1)

def restore_all(target: str, yes: bool = False):
    snap = _pick_snapshot(target)
    log(f"restore from: {snap.name}")
    if not yes:
        confirm = input("  This will overwrite current files. Type 'yes' to confirm: ")
        if confirm.strip().lower() != "yes":
            log("cancelled"); return

    with tarfile.open(snap, "r:gz") as tar:
        tar.extractall(path=WORKSPACE)
        # openclaw.json goes one level up
        try:
            member = tar.getmember("openclaw.json")
            with tar.extractfile(member) as f:
                OPENCLAW_JSON.write_bytes(f.read())
            log("  restored: openclaw.json")
        except KeyError:
            pass

    log(f"restore complete from {snap.name}")

def restore_file(target: str, file_path: str, yes: bool = False):
    snap = _pick_snapshot(target)
    log(f"restore '{file_path}' from: {snap.name}")
    if not yes:
        confirm = input(f"  Overwrite {file_path}? Type 'yes' to confirm: ")
        if confirm.strip().lower() != "yes":
            log("cancelled"); return

    with tarfile.open(snap, "r:gz") as tar:
        try:
            member = tar.getmember(file_path)
        except KeyError:
            log(f"'{file_path}' not found in snapshot"); sys.exit(1)
        dest = WORKSPACE / file_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        with tar.extractfile(member) as f:
            dest.write_bytes(f.read())

    log(f"restored: {file_path}")

# ── CLI ───────────────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(description="dinomem workspace backup + restore")
    p.add_argument("--list", action="store_true", help="List available snapshots")
    p.add_argument("--restore", nargs="?", const="latest", metavar="SNAPSHOT",
                   help="Restore snapshot (default: latest). Pass name or index from --list.")
    p.add_argument("--file", metavar="PATH",
                   help="Restore a single file from snapshot instead of all")
    p.add_argument("--restore-crons", nargs="?", const="latest", metavar="SNAPSHOT",
                   help="Restore cron jobs from a cron backup JSON")
    p.add_argument("--agent", metavar="AGENT_ID",
                   help="Filter cron restore to specific agent (e.g. analyst)")
    p.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt")
    args = p.parse_args()

    if args.list:
        list_snapshots()
        list_cron_backups()
    elif args.restore_crons is not None:
        restore_crons(args.restore_crons, agent=args.agent, yes=args.yes)
    elif args.restore is not None:
        if args.file:
            restore_file(args.restore, args.file, yes=args.yes)
        else:
            restore_all(args.restore, yes=args.yes)
    else:
        log(f"workspace: {WORKSPACE}")
        create_snapshot()

if __name__ == "__main__":
    main()
