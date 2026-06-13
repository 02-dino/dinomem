#!/usr/bin/env python3
"""
workspace_backup.py — Weekly snapshot backup for dinomem workspace.

Backs up only irreplaceable files (memory, config, root files).
Skips regenerable data (vector DBs, session archives, logs, caches).
Keeps last 3 snapshots — auto-rotates oldest.

Run: python3 procedures/workspace_backup.py
Cron: weekly Sunday 2:00 UTC (registered by install.sh)
"""

import os
import sys
import shutil
import tarfile
from pathlib import Path
from datetime import datetime

WORKSPACE = Path(__file__).parent.parent
BACKUP_DIR = WORKSPACE / ".backups" / "snapshots"
KEEP = 3

# Files/dirs to include (relative to workspace)
INCLUDE = [
    "memory",
    "MEMORY.md",
    "AGENTS.md",
    "SOUL.md",
    "IDENTITY.md",
    "TOOLS.md",
    "USER.md",
    "HEARTBEAT.md",
]

# Also backup openclaw.json (one level up from workspace)
OPENCLAW_JSON = WORKSPACE.parent / "openclaw.json"

# Dirs to explicitly skip if included via parent
SKIP_PATTERNS = [
    "kb/vector_db",
    "kb/vector_db_docs",
    "sessions",
    "logs",
    "__pycache__",
    ".backups",
    ".git",
]

def log(msg):
    print(f"[workspace_backup] {msg}")

def should_skip(path: Path) -> bool:
    rel = str(path.relative_to(WORKSPACE))
    return any(rel.startswith(p) for p in SKIP_PATTERNS)

def create_snapshot() -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    archive = BACKUP_DIR / f"snapshot-{ts}.tar.gz"

    with tarfile.open(archive, "w:gz") as tar:
        # Workspace files
        for item in INCLUDE:
            path = WORKSPACE / item
            if path.exists():
                tar.add(path, arcname=item)
                log(f"  + {item}")
            else:
                log(f"  - {item} (not found, skipped)")

        # openclaw.json
        if OPENCLAW_JSON.exists():
            tar.add(OPENCLAW_JSON, arcname="openclaw.json")
            log(f"  + openclaw.json")

    size_kb = archive.stat().st_size // 1024
    log(f"snapshot: {archive.name} ({size_kb} KB)")
    return archive

def rotate():
    snapshots = sorted(BACKUP_DIR.glob("snapshot-*.tar.gz"), reverse=True)
    for old in snapshots[KEEP:]:
        old.unlink()
        log(f"pruned: {old.name}")

def main():
    log(f"workspace: {WORKSPACE}")
    log(f"backup dir: {BACKUP_DIR}")

    archive = create_snapshot()
    rotate()

    remaining = sorted(BACKUP_DIR.glob("snapshot-*.tar.gz"), reverse=True)
    log(f"snapshots kept: {len(remaining)}/{KEEP}")
    for s in remaining:
        log(f"  {s.name} ({s.stat().st_size // 1024} KB)")

if __name__ == "__main__":
    main()
