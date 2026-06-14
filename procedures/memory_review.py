#!/usr/bin/env python3
"""
memory_review.py — Full review of memory files with LLM.

No marking. No flags. Actual review.

Age is filename-based (YYYY-MM-DD.md), not mtime. This is intentional because
memory files are named by the date they were created, and mtime changes on edits.

For each file at review age:
1. Read file content
2. Send to LLM with current date
3. LLM classifies each entry: valid | invalidated | uncertain | noise
4. Rewrite file with reviewed entries (noise removed)
5. Delete file if entirely redundant

Output format: machine-readable tagged lines compatible with OpenClaw memory indexing.

Age buckets (full review triggers):
- 7 days:   Full review
- 14 days:  Full review
- 30 days:  Full review
- 60 days:  Full review
- 120 days: Full review

Deletion at 180 days is handled by cleanup_old_data.py — NOT this script.

Deduplication: .review_tracker.json prevents re-reviewing at same bucket.
"""

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

WORKSPACE = Path("DINOMEM_WORKSPACE_PLACEHOLDER")
MEMORY_DIR = WORKSPACE / "memory"
REVIEW_TRACKER = MEMORY_DIR / ".review_tracker.json"
REPORT_LOG = MEMORY_DIR / ".review_reports.log"

AGE_BUCKETS = [
    (7, "review"),
    (14, "review"),
    (30, "review"),
    (60, "review"),
    (120, "review"),
]


def load_tracker():
    if REVIEW_TRACKER.exists():
        with open(REVIEW_TRACKER, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_tracker(tracker):
    with open(REVIEW_TRACKER, "w", encoding="utf-8") as f:
        json.dump(tracker, f, indent=2)


def get_file_age(filepath):
    """Age is based on filename date (YYYY-MM-DD), not mtime."""
    file_date = filepath.stem
    try:
        file_dt = datetime.strptime(file_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        today = datetime.now(timezone.utc)
        return (today - file_dt).days
    except ValueError:
        return None


def _find_openclaw_bin():
    """Find openclaw binary: PATH first, then common install locations."""
    import shutil
    found = shutil.which("openclaw")
    if found:
        return found
    candidates = [
        "/home/linuxbrew/.linuxbrew/bin/openclaw",
        "/usr/local/bin/openclaw",
        "/usr/bin/openclaw",
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    return "openclaw"  # fallback, let subprocess raise if missing

def call_llm(prompt, max_tokens=4000):
    """Call LLM via OpenClaw gateway."""
    try:
        result = subprocess.run(
            [
                _find_openclaw_bin(),
                "capability", "model", "run",
                "--prompt", prompt,
                "--gateway",
                "--json",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0:
            output = json.loads(result.stdout)
            if output.get("ok") and output.get("outputs"):
                return output["outputs"][0].get("text", "")
    except Exception:
        pass
    return None


def review_file_with_llm(filepath, age):
    """Send file to LLM for full review. Returns reviewed content or None if failed."""
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    if not content.strip():
        return None

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    file_date = filepath.stem

    prompt = f"""You are reviewing a memory file for an AI agent.

File date: {file_date}
Review date: {today}
File age: {age} days

Memory file content:
---
{content}
---

Instructions:
1. For each entry, classify it:
   - [valid] — still accurate and useful. Keep the original text.
   - [invalidated] — wrong, expired, or the opposite happened. Keep the original text and add a brief outcome note.
   - [uncertain] — cannot verify. Keep as [uncertain] with original text.
   - [noise] — low-signal, vague, no actionable insight, or just chitchat. REMOVE it (do not include in output).

2. Be AGGRESSIVE about noise removal. If an entry is not useful to this agent, remove it.
   Examples of noise: generic commentary, vague predictions without specifics, restatements of known facts without new insight, social chat, "interesting" observations that don't affect decisions.

3. If ALL entries are redundant or noise, return exactly: ALL_REDUNDANT

4. Return ONLY the reviewed entries. One per line. Format:
   - [status] original text

5. For invalidated entries: briefly note why — what changed, what actually happened, or why it's no longer true.

6. For expired time-sensitive entries: note the expiry and what actually happened if known.

7. For framework/workflow facts that appear in newer files: they are redundant. Omit them.

8. Be concise. Do not add commentary outside the tagged lines.

9. Preserve the original text as much as possible. Only change the tag and add a brief outcome note for invalidated entries."""

    return call_llm(prompt, max_tokens=4000)


def review():
    today_display = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    tracker = load_tracker()

    md_files = [f for f in MEMORY_DIR.glob("*.md") if f.name != "MEMORY.md" and not f.name.startswith("_")]
    files_reviewed = 0
    files_redundant = 0
    files_failed = 0
    all_changes = []

    for filepath in md_files:
        age = get_file_age(filepath)
        if age is None:
            continue

        file_key = str(filepath)

        # Find highest applicable bucket
        applicable_bucket = None
        applicable_action = None
        for days, action in AGE_BUCKETS:
            if age >= days:
                applicable_bucket = days
                applicable_action = action

        if applicable_bucket is None:
            continue

        # Deduplication: skip if already reviewed at this bucket or higher
        if file_key in tracker:
            last_reviewed = tracker[file_key]
            if last_reviewed >= applicable_bucket:
                continue

        # Full review with LLM
        llm_output = review_file_with_llm(filepath, age)

        if llm_output is None:
            files_failed += 1
            all_changes.append(f"FAILED: {filepath.name} (LLM call failed)")
            # Don't record in tracker — retry next run
            continue

        if llm_output.strip() == "ALL_REDUNDANT":
            try:
                filepath.unlink()
                files_redundant += 1
                all_changes.append(f"REDUNDANT: {filepath.name} (all entries noise/redundant)")
            except Exception as e:
                all_changes.append(f"REDUNDANT_ERROR: {filepath.name}: {e}")
            if file_key in tracker:
                del tracker[file_key]
            save_tracker(tracker)
            continue

        # Write reviewed content
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(llm_output.strip() + "\n")
            files_reviewed += 1
            tracker[file_key] = applicable_bucket
            save_tracker(tracker)
            all_changes.append(f"REVIEWED: {filepath.name} (age {age}d)")
        except Exception as e:
            all_changes.append(f"WRITE_ERROR: {filepath.name}: {e}")

    save_tracker(tracker)

    summary = f"""
=== MEMORY REVIEW REPORT ({today_display}) ===

Files reviewed: {files_reviewed}
Files redundant/noise: {files_redundant}
Files failed: {files_failed}

Changes:
"""
    for change in all_changes:
        summary += f"  {change}\n"

    print(summary)

    with open(REPORT_LOG, "a", encoding="utf-8") as f:
        f.write(summary + "\n")

    return summary


if __name__ == "__main__":
    review()
