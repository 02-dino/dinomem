#!/usr/bin/env python3
"""
memory_cleanup.py — Lightweight deduplication + bootcheck cleanup + MEMORY.md size trim.

What it does:
1. Removes duplicate [factual] entries across memory files (keeps earliest)
2. Auto-deduplicates known framework recitation (workflow_market, analysis_template, etc.)
3. Removes redundant bootcheck-only files (no new facts, just framework recitation)
4. Trims MEMORY.md index if over MAX_INDEX_CHARS — removes oldest entries only.
   Raw data in memory/*.md is never touched. Index can be rebuilt anytime.

What it does NOT do: stale data flagging, prediction expiry, uncertain cleanup.
That is handled by memory_review.py and OpenClaw's own memory system.

Run: python3 procedures/memory_cleanup.py
"""

import re
from datetime import datetime, timezone
from pathlib import Path
from difflib import SequenceMatcher

WORKSPACE = Path("DINOMEM_WORKSPACE_PLACEHOLDER")
MEMORY_DIR = WORKSPACE / "memory"
ARCHIVE_DIR = WORKSPACE / ".memory_archive"
MEMORY_INDEX = WORKSPACE / "MEMORY.md"

SIM_THRESHOLD = 0.80
MAX_INDEX_CHARS = 18000  # 90% of default maxBootstrapFileChars (20000)

KNOWN_FRAMEWORK_FACTS = [
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
            elif re.match(r'^\s*-\s*\[(pattern|preference|uncertain|prediction|valid|invalidated|lesson)\]', line):
                file_has_new_facts = True
            new_lines.append(line)

        cleaned = '\n'.join(new_lines)
        is_redundant = False
        if not file_has_new_facts and len(new_lines) > 3:
            bootcheck_keywords = ['bootcheck', 'framework validation', 'operational procedures',
                                  'workflow_market', 'analysis_template', 'AI successfully recalled']
            if any(kw in cleaned.lower() for kw in bootcheck_keywords):
                is_redundant = True

        if is_redundant:
            with open(ARCHIVE_DIR / f"{md_file.stem}_bootcheck_{today_str}.md", 'a', encoding='utf-8') as f:
                f.write(f"# Bootcheck removal from {md_file.name} on {today_display}\n\n")
                f.write(cleaned + '\n')
            md_file.unlink()
            bootcheck_removed += 1
            continue

        with open(md_file, 'w', encoding='utf-8') as f:
            f.write(cleaned)

        if removed_parts:
            with open(ARCHIVE_DIR / f"{md_file.stem}_dedup_{today_str}.md", 'a', encoding='utf-8') as f:
                f.write(f"# Removed from {md_file.name} on {today_display}\n")
                f.write('\n'.join(removed_parts) + '\n')

    print(f"Memory dedup: {removed_count} duplicates removed, {bootcheck_removed} bootcheck files removed from {len(md_files)} files.")

    trim_memory_index()


def trim_memory_index():
    """
    Trim MEMORY.md index if over MAX_INDEX_CHARS.
    Removes oldest entries (top of index) until under limit.
    Raw data in memory/*.md is never touched — index can be rebuilt anytime.
    """
    if not MEMORY_INDEX.exists():
        return

    content = MEMORY_INDEX.read_text(encoding="utf-8")
    if len(content) <= MAX_INDEX_CHARS:
        print(f"MEMORY.md: {len(content)} chars — under limit, no trim needed.")
        return

    print(f"MEMORY.md: {len(content)} chars exceeds limit ({MAX_INDEX_CHARS}) — trimming oldest entries...")

    lines = content.splitlines()

    # Find where index entries start (lines starting with [TAG])
    header_end = 0
    for i, line in enumerate(lines):
        if re.match(r'^\[[\w]+\]', line.strip()):
            header_end = i
            break

    header = lines[:header_end]
    entries = lines[header_end:]

    removed = 0
    while entries and len("\n".join(header + entries)) > MAX_INDEX_CHARS:
        entries.pop(0)
        removed += 1

    MEMORY_INDEX.write_text("\n".join(header + entries), encoding="utf-8")
    print(f"Trimmed {removed} oldest entries from MEMORY.md. Raw data in memory/*.md untouched.")
    print(f"To rebuild full index: python3 procedures/extract_memory.py --rebuild")


if __name__ == "__main__":
    cleanup()
