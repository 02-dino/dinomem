#!/usr/bin/env python3
"""
_file_backup.py — the single pre-write file backup primitive (dinomem).

WHY THIS EXISTS: config_tool.py and skill_tool.py each carried their own copy of
    BACKUP_SCRIPT = WORKSPACE.parent.parent / "scripts/file-backup.sh"
    if BACKUP_SCRIPT.exists(): subprocess.run([...])
Two problems, both measured 2026-10-22:

  1. THE PATH WAS WRONG AND THE FAILURE WAS SILENT. For a workspace at
     ~/.openclaw/workspace-<agent>, `parent.parent` resolves to ~/ — so the
     helper looked for ~/scripts/file-backup.sh. That file does not exist (the
     real one lives under ~/.openclaw/scripts/), and `file-backup.sh` is not
     shipped by this repo at all. The guard `if BACKUP_SCRIPT.exists()` then
     turned every backup into a NO-OP that still returned success. So
     `skill_tool.py remove` ran `_backup()` and then `shutil.rmtree()` with
     nothing actually saved.

  2. IT WAS DUPLICATED. One primitive, two copies, wrong in both — the exact
     failure mode the build-quality DRY floor exists to prevent: the fix would
     otherwise have to be applied twice and could silently miss a site.

DESIGN: no external script, no shell. A backup is a file copy; making it depend
on an optional shell script is what allowed it to silently vanish. This is
pure stdlib so it CANNOT be absent, and it reports whether it actually wrote
something instead of pretending.

Backups land in <workspace>/.backups/ as <name>.<UTC timestamp>.bak, newest-last,
pruned to KEEP_PER_FILE per original path so a hot file cannot fill the disk.
"""

import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

KEEP_PER_FILE = 10          # retained copies per original path
BACKUP_DIRNAME = ".backups"


def _workspace_for(path):
    """Resolve the workspace that owns `path`.

    Walks up looking for the dinomem layout marker (a procedures/ dir) instead
    of assuming a fixed depth — the assumption of a fixed depth is precisely
    what broke the previous implementation.
    """
    p = Path(path).resolve()
    for parent in p.parents:
        if (parent / "procedures").is_dir() or (parent / "AGENTS.md").is_file():
            return parent
    return p.parent


def backup_file(path, workspace=None):
    """Copy `path` aside before it is modified or deleted.

    Returns the backup Path on success, or None when there was nothing to back
    up (missing source). Never raises: a backup failure must not block the
    caller's real work — but it must also never be mistaken for success, which
    is why the return value is checked by callers that delete.
    """
    src = Path(path)
    if not src.is_file():
        return None
    try:
        ws = Path(workspace) if workspace else _workspace_for(src)
        dest_dir = ws / BACKUP_DIRNAME
        dest_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = dest_dir / f"{src.name}.{stamp}.bak"
        shutil.copy2(src, dest)
        _prune(dest_dir, src.name)
        return dest
    except OSError:
        return None


def _prune(dest_dir, stem):
    """Keep only the newest KEEP_PER_FILE backups for one original filename."""
    try:
        copies = sorted(dest_dir.glob(f"{stem}.*.bak"))
        for old in copies[:-KEEP_PER_FILE]:
            try:
                old.unlink()
            except OSError:
                pass
    except OSError:
        pass


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("usage: _file_backup.py <file> [...]", file=sys.stderr)
        raise SystemExit(2)
    rc = 0
    for arg in sys.argv[1:]:
        made = backup_file(arg)
        if made:
            print(f"backed up {arg} -> {made}")
        else:
            print(f"NOT backed up: {arg}", file=sys.stderr)
            rc = 1
    raise SystemExit(rc)
