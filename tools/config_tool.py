#!/usr/bin/env python3
"""
config_tool.py — Safe writer for agent root config files.

LLM detects intent, generates content, calls this script.
This script handles: backup, validation, dedup, conflict detection, append/patch/remove/write.

Usage (CLI):
  python3 tools/config_tool.py append SOUL.md "tone: concise"
  python3 tools/config_tool.py patch AGENTS.md "my_rule" "rule_content_here"
  python3 tools/config_tool.py remove AGENTS.md "my_rule"
  python3 tools/config_tool.py append TOOLS.md "<yaml block>"
  python3 tools/config_tool.py write IDENTITY.md "<full content>"

Usage (import):
  from tools.config_tool import append_to, patch_section, remove_section, write_file

## CONTENT WRITING PRINCIPLES (for LLM generating content to pass to this script)
Root files are injected into LLM context every turn. Every character costs tokens.
Write content that is lean, precise, and machine-parseable.

Rules:
  - Machine-first: write for LLM parsing, not human reading. Key: value over prose.
  - One rule = one line. Needs a paragraph = too verbose, compress it.
  - No examples: if the rule is unambiguous, drop the example entirely.
  - No redundant symbols: no decorative dashes, arrows, bullets unless structurally required.
  - No aesthetic padding: no "---" dividers, no empty headers, no trailing whitespace.
  - No preamble: never start with "This section describes..." or "The following rules...".
  - No duplicate intent: two rules saying the same thing from different angles = merge to one.
  - No dead rules: behavior already default or obvious = omit.
  - Flatten nesting: max 2 levels of indent. Deeper = restructure.
  - Shortest form that preserves full meaning and reliability. When in doubt, cut.

## WHAT BELONGS IN ROOT FILES vs OUTSIDE
Root files = always-on behavioral config. Only put things here that the agent needs every single turn.

BELONGS in root files:
  - Routing rules: when to use which tool
  - Behavioral constraints: what to never do
  - User preferences: tone, language, format
  - Identity: persona, name, role
  - Active tool specs: path, inputs, when_to_use

DOES NOT BELONG in root files (put elsewhere):
  - One-time setup instructions → README or docs/
  - Historical context or past decisions → memory/*.md
  - Long reference docs, legal text, contracts → docs/ + RAG
  - Tool implementation details (how it works internally) → inline comments in the script
  - Workflow examples or tutorials → README
  - Anything only needed occasionally → memory/*.md or docs/

REMOVE from root files if:
  - The rule hasn't been triggered in months
  - It describes behavior that is already default
  - It duplicates something already in another root file
  - It was added for a one-time task and never generalized
  - It's longer than 3 lines and could be a memory entry instead
"""

import argparse
import subprocess
from difflib import SequenceMatcher
from pathlib import Path

WORKSPACE = Path("DINOMEM_WORKSPACE_PLACEHOLDER")
BACKUP_SCRIPT = WORKSPACE.parent.parent / "scripts/file-backup.sh"

ALLOWED_FILES = {"SOUL.md", "IDENTITY.md", "AGENTS.md", "TOOLS.md", "USER.md"}

DEDUP_THRESHOLD = 0.85
MAX_FILE_CHARS = 20000   # matches agents.defaults.maxBootstrapFileChars default
MAX_TOTAL_CHARS = 60000  # matches agents.defaults.maxBootstrapTotalChars default
WARN_FILE_CHARS = 15000  # warn at 75% of limit
WARN_TOTAL_CHARS = 50000 # warn at 83% of limit  # similarity ratio above which content is considered duplicate

# ── Helpers ───────────────────────────────────────────────────────────────────
def backup(path):
    if BACKUP_SCRIPT.exists():
        subprocess.run([str(BACKUP_SCRIPT), str(path)], capture_output=True)

def check_size(filename, new_content):
    """Check if writing new_content would exceed per-file or total bootstrap limits.
    Returns list of warning strings (empty = ok).
    """
    warnings = []
    path = WORKSPACE / filename
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    projected = len(existing) + len(new_content)

    if projected > MAX_FILE_CHARS:
        warnings.append(
            f"{filename} will be {projected} chars after write — exceeds maxBootstrapFileChars ({MAX_FILE_CHARS}). "
            f"Content beyond limit won't be injected into context (root files are injected every turn — smaller = faster + more reliable). "
            f"Ask your agent to review and clean up {filename} — it can trim outdated sections, merge duplicates, "
            f"rewrite verbose rules, remove dead rules, flatten deep nesting, and restructure sections. "
            f"Or do it manually: one rule = one line; no redundant symbols or padding; no self-evident examples; no prose."
        )
    elif projected > WARN_FILE_CHARS:
        warnings.append(
            f"{filename} will be {projected} chars after write — approaching maxBootstrapFileChars ({MAX_FILE_CHARS}). "
            f"Root files are injected every turn — keep them lightweight. "
            f"Ask your agent to review and tighten {filename} — it can rewrite, merge, and remove rules to keep it lean. "
            f"Or manually: one rule = one line; no redundant symbols; no self-evident examples; flatten deep nesting."
        )

    # Total across all root files
    total = projected
    for f in ALLOWED_FILES:
        if f == filename:
            continue
        p = WORKSPACE / f
        if p.exists():
            total += len(p.read_text(encoding="utf-8"))
    if total > MAX_TOTAL_CHARS:
        warnings.append(
            f"Total root files will be {total} chars — exceeds maxBootstrapTotalChars ({MAX_TOTAL_CHARS}). "
            f"Some files won't be fully injected (all root files load every turn — total size matters). "
            f"Check sizes: wc -c {' '.join(ALLOWED_FILES)} — then ask your agent to review and clean up the largest files. "
            f"It can trim, merge, rewrite, and restructure. Or manually: one rule = one line; no redundant symbols or padding."
        )
    elif total > WARN_TOTAL_CHARS:
        warnings.append(
            f"Total root files will be {total} chars — approaching maxBootstrapTotalChars ({MAX_TOTAL_CHARS}). "
            f"All root files load every turn — keep total size lean. "
            f"Check sizes: wc -c {' '.join(ALLOWED_FILES)} — consider trimming soon."
        )

    return warnings

def validate(content):
    return "\x00" not in content and len(content) <= 50_000

def _similarity(a, b):
    return SequenceMatcher(None, a.strip(), b.strip()).ratio()

def _is_duplicate(content, existing):
    """Check if content is already present or highly similar to any block in existing."""
    content_clean = content.strip()
    # Exact match
    if content_clean in existing:
        return True, "exact"
    # Fuzzy match — split existing into paragraphs and compare
    blocks = [b.strip() for b in existing.split("\n\n") if b.strip()]
    for block in blocks:
        if _similarity(content_clean, block) >= DEDUP_THRESHOLD:
            return True, f"similar ({_similarity(content_clean, block):.0%} match)"
    return False, None

def _find_section(lines, section_key):
    """Find start/end line index of a top-level section by key."""
    start = next(
        (i for i, l in enumerate(lines) if l.startswith(f"{section_key}:") or l.startswith(f"## {section_key}")),
        None
    )
    if start is None:
        return None, None
    end = next(
        (i for i in range(start + 1, len(lines))
         if lines[i] and (lines[i][0].isalpha() or lines[i].startswith("##")) and ":" in lines[i]),
        len(lines)
    )
    return start, end

# ── Operations ────────────────────────────────────────────────────────────────
def append_to(filename, content):
    """
    Append a block to a root config file.
    Skips if content is duplicate or highly similar to existing content.
    """
    if filename not in ALLOWED_FILES:
        return {"ok": False, "error": f"Not allowed: {filename}. Allowed: {sorted(ALLOWED_FILES)}"}
    if not validate(content):
        return {"ok": False, "error": "Content failed validation (null bytes or too large)"}

    path = WORKSPACE / filename
    existing = path.read_text(encoding="utf-8") if path.exists() else ""

    is_dup, reason = _is_duplicate(content, existing)
    if is_dup:
        return {"ok": True, "file": filename, "action": "skip", "reason": f"duplicate: {reason}"}

    size_warnings = check_size(filename, content)

    backup(path)
    sep = "\n" if existing and not existing.endswith("\n\n") else ""
    path.write_text(existing + sep + content.strip() + "\n", encoding="utf-8")
    result = {"ok": True, "file": filename, "action": "append"}
    if size_warnings:
        result["warnings"] = size_warnings
    return result

def patch_section(filename, section_key, content):
    """
    Replace an existing section (by key) or append if not found.
    Handles both YAML key format and ## heading format.
    """
    if filename not in ALLOWED_FILES:
        return {"ok": False, "error": f"Not allowed: {filename}"}
    if not validate(content):
        return {"ok": False, "error": "Content failed validation"}

    path = WORKSPACE / filename
    if not path.exists():
        return append_to(filename, content)

    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    start, end = _find_section(lines, section_key)

    if start is None:
        return append_to(filename, content)

    size_warnings = check_size(filename, content)
    backup(path)
    path.write_text("".join(lines[:start] + [content.strip() + "\n"] + lines[end:]), encoding="utf-8")
    result = {"ok": True, "file": filename, "action": "patch", "section": section_key}
    if size_warnings:
        result["warnings"] = size_warnings
    return result

def remove_section(filename, section_key):
    """
    Remove a section by key from a root config file.
    Handles both YAML key format and ## heading format.
    """
    if filename not in ALLOWED_FILES:
        return {"ok": False, "error": f"Not allowed: {filename}"}

    path = WORKSPACE / filename
    if not path.exists():
        return {"ok": False, "error": f"{filename} not found"}

    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    start, end = _find_section(lines, section_key)

    if start is None:
        return {"ok": False, "error": f"Section '{section_key}' not found in {filename}"}

    backup(path)
    # Also remove trailing blank line if present
    new_lines = lines[:start] + lines[end:]
    path.write_text("".join(new_lines), encoding="utf-8")
    return {"ok": True, "file": filename, "action": "remove", "section": section_key}

def write_file(filename, content):
    """Full overwrite. Use only for IDENTITY.md or when explicitly replacing entire file."""
    if filename not in ALLOWED_FILES:
        return {"ok": False, "error": f"Not allowed: {filename}"}
    if not validate(content):
        return {"ok": False, "error": "Content failed validation"}

    path = WORKSPACE / filename
    backup(path)
    path.write_text(content.strip() + "\n", encoding="utf-8")
    return {"ok": True, "file": filename, "action": "write"}

# ── CLI ───────────────────────────────────────────────────────────────────────
def main():
    import json
    p = argparse.ArgumentParser(description="Safe writer for agent root config files")
    sub = p.add_subparsers(dest="cmd")

    for cmd in ["append", "write"]:
        s = sub.add_parser(cmd)
        s.add_argument("file")
        s.add_argument("content")

    s = sub.add_parser("patch")
    s.add_argument("file")
    s.add_argument("section_key")
    s.add_argument("content")

    s = sub.add_parser("remove")
    s.add_argument("file")
    s.add_argument("section_key")

    args = p.parse_args()

    if args.cmd == "append":
        print(json.dumps(append_to(args.file, args.content)))
    elif args.cmd == "patch":
        print(json.dumps(patch_section(args.file, args.section_key, args.content)))
    elif args.cmd == "remove":
        print(json.dumps(remove_section(args.file, args.section_key)))
    elif args.cmd == "write":
        print(json.dumps(write_file(args.file, args.content)))
    else:
        p.print_help()

if __name__ == "__main__":
    main()
