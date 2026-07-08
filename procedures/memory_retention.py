#!/usr/bin/env python3
"""
memory_retention.py — deterministic, zero-LLM verdict-aware retention.

This is the DELETER half of the retention system. It does NOT call an LLM and
makes NO judgments of its own — it only reads the per-entry verdict tags that
memory_review.py already wrote, plus file age (filename date), and applies ONE
rule:

    At terminal age (default 180d), an entry that is NOT [valid] is deleted.
    [valid] is the only verdict that earns keeping.

Rationale (see _note_upcay-guillotine-fix-brainstorm.md, LOCKED design):
- Symmetric with freeze: valid -> keep/freeze; not-valid -> delete. Two states,
  one axis. No 2-strike counter, no uncertain special-case, no counters.
- A file had 5 review cycles (7/14/30/60/120d) to earn a [valid] tag. By the
  terminal age it either graduated to FROZEN (all [valid]) or it did not.
- no-verdict-tag = not-valid. A never-reviewed entry (LLM kept failing for
  180d) has no [valid] tag, so it is treated as not-valid and removed. That is
  the safety net for the never-reviewed case (such a file is almost certainly
  junk anyway).

SKIPS (never touched):
- FROZEN files (first non-empty line is the frozen marker) — immortal.
- _pin_*.md and _note_*.md and any _*.md (pins / notes / neuron-managed).
- MEMORY.md.
- Files whose age cannot be determined from a YYYY-MM-DD filename.
- Files younger than the terminal age.

GRANULARITY (per-entry): at terminal age, not-valid entries are pruned in place.
If NOTHING [valid] survives, the whole file is unlinked. If some [valid]
entries remain, the file is kept with only its [valid] entries. This is the
backstop path; in normal operation memory_review.py already prunes not-valid
entries during its rewrites, so this file mostly unlinks fully-dead files.

Base file: ships to every install. Neuron adds a connectivity keep-bias on top
(memory_graph.py) which may rescue not-valid hubs BEFORE this runs; that is a
neuron-only pre-pass, not this file's concern.
"""

import argparse
import os
import re
from datetime import datetime, timezone
from pathlib import Path

# Workspace resolution mirrors memory_review.py: env override > install-time sed
# > self-locate (procedures/ is one level under the workspace root).
_WS_DEFAULT = "DINOMEM_WORKSPACE_PLACEHOLDER"
if _WS_DEFAULT.startswith("DINOMEM_"):  # sed did not run
    _WS_DEFAULT = str(Path(__file__).resolve().parent.parent)
WORKSPACE = Path(os.environ.get("DINOMEM_WORKSPACE", _WS_DEFAULT))
MEMORY_DIR = WORKSPACE / "memory"

# Terminal age: an entry that is not [valid] by this age is deleted.
TERMINAL_AGE_DAYS = int(os.environ.get("DINOMEM_TERMINAL_AGE_DAYS", "180"))

# Must match memory_review.py.
FROZEN_MARKER = "<!-- frozen: true -->"

# The one keep verdict. Everything else (including no tag) is not-valid.
VALID_TAG = "[valid]"
VERDICT_TAGS = ("[valid]", "[invalidated]", "[uncertain]", "[noise]")

# A memory entry line: optional leading "- ", then a verdict tag, then text.
# We only prune lines that look like verdict-tagged entries; structural lines
# (headers, blank lines, frontmatter) are left alone unless the whole file dies.
_ENTRY_RE = re.compile(r"^\s*-?\s*(\[valid\]|\[invalidated\]|\[uncertain\]|\[noise\])")
_UNTAGGED_ENTRY_RE = re.compile(r"^\s*-\s+\S")


def is_frozen(filepath: Path) -> bool:
    """Frozen if the first non-empty line is the frozen marker."""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s:
                    continue
                return s == FROZEN_MARKER
    except Exception:
        return False
    return False


def get_file_age(filepath: Path):
    """Age in days from filename YYYY-MM-DD. None if not a dated filename."""
    try:
        file_dt = datetime.strptime(filepath.stem, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - file_dt).days


def _line_is_valid_entry(line: str) -> bool:
    """True only for a line explicitly tagged [valid]."""
    m = _ENTRY_RE.match(line)
    return bool(m) and m.group(1) == VALID_TAG


def _line_is_entry(line: str) -> bool:
    """True if the line is an entry (tagged or a bullet). Untagged bullet = not-valid."""
    return bool(_ENTRY_RE.match(line)) or bool(_UNTAGGED_ENTRY_RE.match(line))


def prune_file(filepath: Path, dry_run: bool = False):
    """
    Prune not-valid entries from a terminal-age file.

    Returns a dict: {action, removed_entries, kept_entries}.
    action in {"unlink", "prune", "keep", "skip"}.
    """
    try:
        text = filepath.read_text(encoding="utf-8")
    except Exception as e:
        return {"action": "skip", "error": str(e), "removed_entries": 0, "kept_entries": 0}

    lines = text.split("\n")
    kept_lines = []
    valid_count = 0
    removed_entries = 0

    for line in lines:
        if _line_is_entry(line):
            if _line_is_valid_entry(line):
                valid_count += 1
                kept_lines.append(line)
            else:
                # not-valid entry (bad verdict OR untagged bullet) -> drop
                removed_entries += 1
            continue
        # Non-entry structural line: keep for now (may be dropped if file dies).
        kept_lines.append(line)

    if valid_count == 0:
        # Nothing earned keeping -> unlink whole file.
        if not dry_run:
            try:
                filepath.unlink()
            except Exception as e:
                return {"action": "skip", "error": str(e),
                        "removed_entries": removed_entries, "kept_entries": 0}
        return {"action": "unlink", "removed_entries": removed_entries, "kept_entries": 0}

    if removed_entries == 0:
        return {"action": "keep", "removed_entries": 0, "kept_entries": valid_count}

    # Some valid survive, some not-valid pruned -> rewrite with only kept lines.
    if not dry_run:
        new_text = "\n".join(kept_lines).strip() + "\n"
        try:
            filepath.write_text(new_text, encoding="utf-8")
        except Exception as e:
            return {"action": "skip", "error": str(e),
                    "removed_entries": removed_entries, "kept_entries": valid_count}
    return {"action": "prune", "removed_entries": removed_entries, "kept_entries": valid_count}


def run(dry_run: bool = False, terminal_age: int = TERMINAL_AGE_DAYS):
    if not MEMORY_DIR.exists():
        print(f"[Retention] memory/ dir not found ({MEMORY_DIR}), nothing to do")
        return {"files_unlinked": 0, "files_pruned": 0, "entries_removed": 0}

    files_unlinked = 0
    files_pruned = 0
    files_kept = 0
    files_skipped = 0
    entries_removed = 0
    changes = []

    for filepath in sorted(MEMORY_DIR.glob("*.md")):
        name = filepath.name
        # Protected: MEMORY.md, any _*.md (pins/notes/neuron-managed)
        if name == "MEMORY.md" or name.startswith("_"):
            continue
        # Frozen files are immortal.
        if is_frozen(filepath):
            files_skipped += 1
            continue
        age = get_file_age(filepath)
        if age is None:
            # Non-dated filename: cannot age -> leave alone (not our jurisdiction).
            files_skipped += 1
            continue
        if age < terminal_age:
            continue

        res = prune_file(filepath, dry_run=dry_run)
        action = res.get("action")
        if action == "unlink":
            files_unlinked += 1
            entries_removed += res["removed_entries"]
            changes.append(f"{'WOULD UNLINK' if dry_run else 'UNLINKED'}: {name} (age {age}d, 0 valid)")
        elif action == "prune":
            files_pruned += 1
            entries_removed += res["removed_entries"]
            changes.append(f"{'WOULD PRUNE' if dry_run else 'PRUNED'}: {name} (age {age}d, -{res['removed_entries']} not-valid, {res['kept_entries']} valid kept)")
        elif action == "keep":
            files_kept += 1
        elif action == "skip":
            files_skipped += 1
            changes.append(f"SKIP(error): {name}: {res.get('error')}")

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    print(f"=== MEMORY RETENTION ({stamp}){' [DRY RUN]' if dry_run else ''} ===")
    print(f"Terminal age: {terminal_age}d")
    print(f"Files unlinked (0 valid): {files_unlinked}")
    print(f"Files pruned (mixed):     {files_pruned}")
    print(f"Files kept (all valid):   {files_kept}")
    print(f"Files skipped:            {files_skipped}")
    print(f"Entries removed:          {entries_removed}")
    for c in changes:
        print(f"  {c}")

    return {
        "files_unlinked": files_unlinked,
        "files_pruned": files_pruned,
        "entries_removed": entries_removed,
    }


def main():
    ap = argparse.ArgumentParser(description="Deterministic verdict-aware memory retention (zero-LLM).")
    ap.add_argument("--dry-run", action="store_true", help="Preview only, delete nothing")
    ap.add_argument("--terminal-age", type=int, default=TERMINAL_AGE_DAYS,
                    help=f"Age (days) at which not-valid entries are deleted (default {TERMINAL_AGE_DAYS})")
    args = ap.parse_args()
    run(dry_run=args.dry_run, terminal_age=args.terminal_age)


if __name__ == "__main__":
    main()
