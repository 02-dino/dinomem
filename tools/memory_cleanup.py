#!/usr/bin/env python3
"""
memory_cleanup.py — Lightweight deduplication + bootcheck cleanup.

What it does:
1. Removes duplicate [factual] entries across memory files (keeps earliest)
2. Auto-deduplicates known framework recitation (workflow_market, analysis_template, etc.)
3. Removes redundant bootcheck-only files (no new facts, just framework recitation)

What it does NOT do: stale data flagging, prediction expiry, uncertain cleanup, MEMORY.md edits.
That is handled by memory_review.py and OpenClaw's own memory system.

Run: python3 memory_cleanup.py
"""

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from difflib import SequenceMatcher

WORKSPACE = Path("DINOMEM_WORKSPACE_PLACEHOLDER")
MEMORY_DIR = WORKSPACE / "memory"
# Archive lives OUTSIDE memory/ so memory-core (memory_search) does NOT index these
# pre-dedup backup snapshots — they're continuity-only, not searchable content.
ARCHIVE_DIR = WORKSPACE / ".memory_archive"

SIM_THRESHOLD = 0.80

# Add agent-specific framework facts here that should be deduplicated.
# These are recitations of your AGENTS.md/workflow structure — not real memories.
# Example: "workflow_market has two main paths", "analysis_template has four sections"
# Leave empty for a generic agent with no custom framework facts.
KNOWN_FRAMEWORK_FACTS = [
    # Generic patterns safe to deduplicate across all agents:
    "framework validation: The AI successfully recalled",
    "framework validation: The AI correctly identified",
    "user expects exact adherence to instructions",
    "user expects structured and detailed recall",
]


def similar(a, b):
    return SequenceMatcher(None, a, b).ratio()


def is_duplicate(text, seen_facts):
    text_lower = text.lower()
    for known in KNOWN_FRAMEWORK_FACTS:
        if known.lower() in text_lower:
            return True
    for seen in seen_facts:
        if similar(text, seen) >= SIM_THRESHOLD:
            return True
    return False


def cleanup():
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_display = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

    md_files = sorted([f for f in MEMORY_DIR.glob("*.md") if f.name != "MEMORY.md" and not f.name.startswith("_")])
    seen_facts = []
    removed_count = 0
    bootcheck_removed = 0

    for md_file in md_files:
        with open(md_file, 'r', encoding='utf-8') as f:
            content = f.read()

        lines = content.split('\n')
        new_lines = []
        removed_parts = []
        file_has_new_facts = False

        for line in lines:
            match = re.match(r'^\s*-\s*\[factual\]\s*(.+?)$', line)
            if match:
                fact = match.group(1).strip()
                if is_duplicate(fact, seen_facts):
                    removed_count += 1
                    removed_parts.append(line)
                    continue
                seen_facts.append(fact)
                file_has_new_facts = True
            # Track non-framework entries as "new facts" — including reviewed tags
            elif re.match(r'^\s*-\s*\[(pattern|preference|uncertain|prediction|valid|invalidated)\]', line):
                file_has_new_facts = True
            new_lines.append(line)

        # After dedup, check if file is just bootcheck/framework recitation
        cleaned = '\n'.join(new_lines)
        is_redundant = False
        if not file_has_new_facts and len(new_lines) > 3:
            # Check if content is mostly bootcheck or framework recitation
            bootcheck_keywords = ['bootcheck', 'framework validation', 'operational procedures',
                                  'workflow_market', 'analysis_template', 'AI successfully recalled']
            content_lower = cleaned.lower()
            if any(kw in content_lower for kw in bootcheck_keywords):
                is_redundant = True

        if is_redundant:
            # Archive the whole file then delete
            with open(ARCHIVE_DIR / f"{md_file.stem}_bootcheck_{today_str}.md", 'a', encoding='utf-8') as f:
                f.write(f"# Bootcheck removal from {md_file.name} on {today_display}\n\n")
                f.write(cleaned + '\n')
            md_file.unlink()
            bootcheck_removed += 1
            continue

        # Rewrite file
        with open(md_file, 'w', encoding='utf-8') as f:
            f.write(cleaned)

        # Archive removed
        if removed_parts:
            with open(ARCHIVE_DIR / f"{md_file.stem}_dedup_{today_str}.md", 'a', encoding='utf-8') as f:
                f.write(f"# Removed from {md_file.name} on {today_display}\n")
                f.write('\n'.join(removed_parts) + '\n')

    print(f"Memory dedup complete: {removed_count} duplicate facts removed, {bootcheck_removed} bootcheck files removed from {len(md_files)} files.")
    print(f"NOTE: MEMORY.md and topics/INDEX.md are managed by OpenClaw. Do not manually regenerate.")


if __name__ == "__main__":
    cleanup()
