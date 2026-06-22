#!/usr/bin/env python3
"""
Cleanup bare daily memory files created by OpenClaw memoryFlush.

Companion for startupContext + daily flush (see README → startupContext + daily flush).

These bare `memory/YYYY-MM-DD.md` files exist ONLY to feed OpenClaw's
startupContext (last-N-days injection on /new and /reset). dinomem itself does
not use them — it reads archived sessions and writes per-item files named
`YYYY-MM-DD_<type>_<slug>.md` (underscore after the date). Once a bare daily
file ages past the startupContext window it is dead weight, so this deletes it.

Deletes ONLY bare `YYYY-MM-DD.md` files older than RETENTION_DAYS.
NEVER touches:
  - MEMORY.md
  - per-item dinomem files (YYYY-MM-DD_<type>_<slug>.md)  <- underscore after date
  - pinned/note files (_pin_*.md, _note_*.md, any _*.md)

Cron (run after dinomem's own cleanup):
    5 2 * * * cd ~/.openclaw/workspace-myagent && python3 procedures/cleanup_startup_daily.py >> logs/cleanup.log 2>&1

Usage:
    python3 procedures/cleanup_startup_daily.py            # delete bare daily > RETENTION_DAYS
    python3 procedures/cleanup_startup_daily.py --dry-run  # preview only
    python3 procedures/cleanup_startup_daily.py --days 2   # override retention (match dailyMemoryDays)
"""

import argparse
import re
from datetime import datetime, timedelta
from pathlib import Path

WORKSPACE = Path("DINOMEM_WORKSPACE_PLACEHOLDER")
MEMORY_DIR = WORKSPACE / "memory"
RETENTION_DAYS = 2  # keep in sync with startupContext.dailyMemoryDays

# Exact bare daily filename: 2026-06-20.md  (nothing after the date)
BARE_DAILY_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.md$")


def find_bare_daily(retention_days):
    cutoff = datetime.now() - timedelta(days=retention_days)
    old = []
    if not MEMORY_DIR.exists():
        return old
    for f in MEMORY_DIR.glob("*.md"):
        m = BARE_DAILY_RE.match(f.name)
        if not m:
            continue  # skips _pin_, _note_, MEMORY.md, and DATE_type_slug.md
        try:
            file_date = datetime.strptime(m.group(1), "%Y-%m-%d")
        except ValueError:
            continue
        if file_date < cutoff:
            old.append(f)
    return old


def main():
    ap = argparse.ArgumentParser(description="Delete bare YYYY-MM-DD.md daily files older than N days")
    ap.add_argument("--days", type=int, default=RETENTION_DAYS, help=f"Retention (default {RETENTION_DAYS})")
    ap.add_argument("--dry-run", action="store_true", help="Preview only")
    args = ap.parse_args()

    old = find_bare_daily(args.days)
    if not old:
        print(f"[StartupDaily] No bare daily files older than {args.days}d")
        return

    print(f"[StartupDaily] Found {len(old)} bare daily file(s) > {args.days}d")
    if args.dry_run:
        for f in old:
            print(f"[StartupDaily] DRY RUN would delete: {f.name}")
        return

    deleted = 0
    for f in old:
        try:
            f.unlink()
            print(f"[StartupDaily] Deleted: {f.name}")
            deleted += 1
        except Exception as e:
            print(f"[StartupDaily] Error deleting {f.name}: {e}")
    print(f"[StartupDaily] Complete: {deleted} file(s) deleted")


if __name__ == "__main__":
    main()
