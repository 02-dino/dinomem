#!/usr/bin/env python3
"""
config_tool.py — Safe writer for agent root config files.

LLM detects intent, generates content, calls this script.
This script handles: backup, validation, pre-write LLM review (dedup/conflict/grouping), append/patch/remove/write.

Usage (CLI):
  python3 tools/config_tool.py append SOUL.md "tone: concise"
  python3 tools/config_tool.py patch AGENTS.md "my_rule" "rule_content_here"
  python3 tools/config_tool.py remove AGENTS.md "my_rule"
  python3 tools/config_tool.py append TOOLS.md "<yaml block>"
  python3 tools/config_tool.py write IDENTITY.md "<full content>"

Usage (import):
  from tools.config_tool import append_to, patch_section, remove_section, write_file

## WRITING PRINCIPLES (for LLM generating content to pass to this script)
Root files are injected into LLM context every turn. Every character costs tokens.

Before writing: read the full target file. Use common sense — dedup, resolve contradictions, group related content. No examples. No notes (no "note:", "NOTE", asides, caveats, or meta-commentary — write the rule itself, not commentary about it). Shortest form that preserves meaning. Machine-readable over human-readable.

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
  - Notes / reminders / todos / time-bound items → memory/_note_*.md (never a root file)
  - Anything only needed occasionally → memory/*.md or docs/

REMOVE from root files if:
  - The rule hasn't been triggered in months
  - It describes behavior that is already default
  - It duplicates something already in another root file
  - It was added for a one-time task and never generalized
  - It's longer than 3 lines and could be a memory entry instead
"""

import argparse
import os
import subprocess
from pathlib import Path

# Workspace resolution (priority): DINOMEM_WORKSPACE env var > install-time sed
# substitution of DINOMEM_WORKSPACE_PLACEHOLDER > self-locate from this file's
# location (tools/ is one level under the workspace root). Self-locate fallback
# keeps the script working if install-time sed was skipped/failed.
_WS_DEFAULT = "DINOMEM_WORKSPACE_PLACEHOLDER"
if _WS_DEFAULT.startswith("DINOMEM_"):  # sed did not run
    _WS_DEFAULT = str(Path(__file__).resolve().parent.parent)
WORKSPACE = Path(os.environ.get("DINOMEM_WORKSPACE", _WS_DEFAULT))
BACKUP_SCRIPT = WORKSPACE.parent.parent / "scripts/file-backup.sh"

ALLOWED_FILES = {"SOUL.md", "IDENTITY.md", "AGENTS.md", "TOOLS.md", "USER.md"}

def _resolve_caps():
    """Read the LIVE bootstrap caps from openclaw.json instead of assuming the
    20000/60000 defaults. install.sh raises agents.defaults.bootstrapMaxChars /
    bootstrapTotalMaxChars to fit injected blocks (+10k headroom); if this tool
    kept warning against a hardcoded 20000 it would false-alarm on every write
    above 20k even when the real cap is higher — nagging the user to trim a file
    that fits fine, the inverse of the memory_cleanup stale-hardcode bug.

    Returns (max_file, max_total). Floors at the historical defaults so this tool
    never warns LATER than before if the config is unreadable/absent: a config
    with caps below default is treated as default (we never relax below 20k/60k).
    Same config-read pattern as extract_memory.py's auto-routing.
    """
    FILE_DEFAULT, TOTAL_DEFAULT = 20000, 60000
    try:
        cfg_path = Path.home() / ".openclaw" / "openclaw.json"
        if not cfg_path.exists():
            return FILE_DEFAULT, TOTAL_DEFAULT
        import json as _json
        with open(cfg_path, "r", encoding="utf-8") as f:
            defaults = _json.load(f).get("agents", {}).get("defaults", {})
        mf = defaults.get("bootstrapMaxChars", FILE_DEFAULT)
        mt = defaults.get("bootstrapTotalMaxChars", TOTAL_DEFAULT)
        mf = mf if isinstance(mf, (int, float)) and mf > 0 else FILE_DEFAULT
        mt = mt if isinstance(mt, (int, float)) and mt > 0 else TOTAL_DEFAULT
        return max(FILE_DEFAULT, int(mf)), max(TOTAL_DEFAULT, int(mt))
    except Exception:
        return FILE_DEFAULT, TOTAL_DEFAULT

MAX_FILE_CHARS, MAX_TOTAL_CHARS = _resolve_caps()  # LIVE caps (>= 20000/60000 floor)
WARN_FILE_CHARS = int(MAX_FILE_CHARS * 0.75)   # warn at 75% of the live per-file cap
WARN_TOTAL_CHARS = int(MAX_TOTAL_CHARS * 0.83)  # warn at 83% of the live total cap

# Remediation ladder carried INSIDE the over-cap warning (not in AGENTS.md, to
# avoid inflating the always-injected prompt). The agent reads this from the
# tool result and must follow it: STOP, ask the human, escalate in order, get
# permission at each step. Never self-resolve, never assume importance.
REMEDIATION_LADDER = (
    "ACTION REQUIRED — do NOT trim/compress or decide importance yourself. STOP and ask the human, "
    "then proceed IN ORDER, one step per human approval: "
    "(1) RESTYLE: rewrite the WHOLE file into the WRITING PRINCIPLES style (one rule=one line, no examples, "
    "no prose, no notes, machine-readable) — lossless, recovers space from un-styled entries; try this first. "
    "(2) COMPRESS: only if already styled and still over — condense phrasing (mildly lossy). "
    "(3) TRIM OUTDATED: only if compression loses too much — you CANNOT judge importance, so present candidate "
    "outdated/unused sections and let the human choose what to cut. "
    "(4) HUMAN EDITS the file directly."
)

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
            f"Content beyond limit won't be injected into context (root files are injected every turn). "
            + REMEDIATION_LADDER
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
            f"Some files won't be fully injected (all root files load every turn). Largest first: wc -c {' '.join(ALLOWED_FILES)}. "
            + REMEDIATION_LADDER
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

def _is_duplicate(content, existing):
    """Exact-match only guard — semantic dedup/conflict handled by LLM pre-write review."""
    return content.strip() in existing, "exact" 

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
    Skips on exact duplicate. Semantic dedup/conflict/grouping handled by LLM pre-write review.
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
