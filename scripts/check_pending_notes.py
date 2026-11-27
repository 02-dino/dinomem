#!/usr/bin/env python3
"""
check_pending_notes.py — Zero-LLM pre-filter for the Pending Note Reminder lane.

Scans memory/_note_*.md for notes that are:
- status: pending
- older than 1 day
- past their remind date: `remind_on: YYYY-MM-DD` if set, else halfway between
  `date` and `stale_after`, else date + 3 days
- not reminded within the cooldown window (default 24h)

Exits 0 with JSON on stdout when notes qualify (cron_gate fires the LLM lane),
1 when nothing qualifies (zero cost).

WHY the cooldown: cron_gate polls every 15 min. Without a record of the last
reminder, a note past its remind date re-qualified on EVERY tick, so the owner
got the same reminder every 15-30 min until the note was closed (measured
2026-11-27). Qualifying notes are stamped in STATE_FILE at emit time.

Env overrides:
  DINOMEM_MEMORY_DIR            memory dir (default: <script>/../memory)
  PENDING_REMINDER_COOLDOWN_H   hours between reminders per note (default 24;
                                0 = no cooldown, the old behaviour)
"""
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

MEMORY_DIR = Path(os.environ.get("DINOMEM_MEMORY_DIR")
                  or Path(__file__).resolve().parent.parent / "memory")
STATE_FILE = MEMORY_DIR / ".pending_reminder_state.json"


def cooldown() -> timedelta:
    try:
        return timedelta(hours=max(0.0, float(os.environ.get("PENDING_REMINDER_COOLDOWN_H", "24"))))
    except ValueError:
        return timedelta(hours=24)


def parse_frontmatter(text: str) -> dict:
    fields = {}
    for line in text.splitlines():
        m = re.match(r"^(\w+):\s*(.+)$", line.strip())
        if m:
            fields.setdefault(m.group(1), m.group(2).strip())
    return fields


def parse_date(s: str):
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d").date()
    except (ValueError, AttributeError):
        return None


def remind_date(meta: dict, created):
    explicit = parse_date(meta.get("remind_on", ""))
    if explicit:
        return explicit
    stale = parse_date(meta.get("stale_after", ""))
    if stale:
        return created + timedelta(days=max(1, (stale - created).days // 2))
    return created + timedelta(days=3)


def load_state() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def recently_reminded(state: dict, name: str, now: datetime, window: timedelta) -> bool:
    try:
        last = datetime.fromisoformat(state[name])
    except (KeyError, TypeError, ValueError):
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return now - last < window


def main():
    now = datetime.now(timezone.utc)
    today = now.date()
    window = cooldown()
    state = load_state()
    qualifying = []
    for f in sorted(MEMORY_DIR.glob("_note_*.md")):
        try:
            text = f.read_text()
        except OSError:
            continue
        meta = parse_frontmatter(text)
        if meta.get("status") != "pending":
            continue
        created = parse_date(meta.get("date", ""))
        if not created or (today - created).days < 1:
            continue
        rd = remind_date(meta, created)
        if today < rd or recently_reminded(state, f.name, now, window):
            continue
        qualifying.append({
            "file": str(f),
            "title": text.splitlines()[0].lstrip("# ").strip() if text else f.stem,
            "date": str(created),
            "stale_after": meta.get("stale_after", ""),
            "done_when": meta.get("done_when", ""),
            "remind_date": str(rd),
        })
    if not qualifying:
        sys.exit(1)
    live = {f.name for f in MEMORY_DIR.glob("_note_*.md")}
    state = {k: v for k, v in state.items() if k in live}  # drop closed notes
    for q in qualifying:
        state[Path(q["file"]).name] = now.isoformat()
    try:
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2))
        tmp.replace(STATE_FILE)
    except OSError:
        pass  # fail-open: a reminder that repeats beats one that is lost
    print(json.dumps(qualifying, indent=2))
    sys.exit(0)


if __name__ == "__main__":
    main()
