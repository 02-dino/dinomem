#!/usr/bin/env python3
"""
check_pending_notes.py — Zero-LLM pre-filter for task_bound note reminder cron.

Scans memory/_note_*.md for task_bound notes that are:
- status: pending
- older than 1 day
- past their remind date (halfway between date and stale_after, fallback 3 days)

Exits 0 with JSON output if notes found (trigger LLM).
Exits 1 if no notes qualify (skip LLM, zero cost).
"""

import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

MEMORY_DIR = Path(__file__).parent.parent / "memory"
TODAY = datetime.utcnow().date()


def parse_frontmatter(text: str) -> dict:
    fields = {}
    for line in text.splitlines():
        m = re.match(r"^(\w+):\s*(.+)$", line.strip())
        if m:
            fields[m.group(1)] = m.group(2).strip()
    return fields


def remind_date(created: datetime.date, stale_after_str: str) -> datetime.date:
    try:
        stale = datetime.strptime(stale_after_str, "%Y-%m-%d").date()
        delta = (stale - created).days
        return created + timedelta(days=max(1, delta // 2))
    except Exception:
        return created + timedelta(days=3)


def main():
    qualifying = []

    for f in sorted(MEMORY_DIR.glob("_note_*.md")):
        text = f.read_text()
        meta = parse_frontmatter(text)

        if meta.get("status") != "pending":
            continue

        try:
            created = datetime.strptime(meta["date"], "%Y-%m-%d").date()
        except Exception:
            continue

        if (TODAY - created).days < 1:
            continue

        rd = remind_date(created, meta.get("stale_after", ""))
        if TODAY < rd:
            continue

        qualifying.append({
            "file": str(f),
            "title": text.splitlines()[0].lstrip("# ").strip(),
            "date": str(created),
            "stale_after": meta.get("stale_after", ""),
            "done_when": meta.get("done_when", ""),
            "remind_date": str(rd),
        })

    if not qualifying:
        sys.exit(1)

    print(json.dumps(qualifying, indent=2))
    sys.exit(0)


if __name__ == "__main__":
    main()
