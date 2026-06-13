#!/usr/bin/env python3
"""
workspace_backup.py — Weekly snapshot backup + restore for dinomem workspace.

Backs up only irreplaceable files (memory, config, root files).
Skips regenerable data (vector DBs, session archives, logs, caches).
Keeps last 3 snapshots — auto-rotates oldest.

Usage:
  python3 procedures/workspace_backup.py                    # create snapshot
  python3 procedures/workspace_backup.py --list             # list snapshots
  python3 procedures/workspace_backup.py --restore          # restore latest (interactive)
  python3 procedures/workspace_backup.py --restore latest   # restore latest (no prompt)
  python3 procedures/workspace_backup.py --restore <file>   # restore specific snapshot
  python3 procedures/workspace_backup.py --restore --file memory/2026-06-01.md  # restore one file

Cron: weekly Sunday 2:00 UTC (registered by install.sh)
"""

import argparse
import os
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

    with tarfile.open(archive, "w:gz") as tar:
        for item in INCLUDE:
            path = WORKSPACE / item
            if path.exists():
                tar.add(path, arcname=item)
                log(f"  + {item}")
        if OPENCLAW_JSON.exists():
            tar.add(OPENCLAW_JSON, arcname="openclaw.json")
            log(f"  + openclaw.json")

    size_kb = archive.stat().st_size // 1024
    log(f"snapshot: {archive.name} ({size_kb} KB)")

    # Rotate
    for old in _snapshots()[KEEP:]:
        old.unlink()
        log(f"pruned: {old.name}")

    return archive

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
    p.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt")
    args = p.parse_args()

    if args.list:
        list_snapshots()
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
