#!/usr/bin/env python3
"""commit_reason.py — the DROP side of two-tier semantic git-commit subjects.

WHY
  A meaningful memory write (a promotion graduating, a fact superseded, a done-note
  resolved, a cross-head dedup-merge) ALREADY knows WHY it happened — that reason
  is a structured string the caller holds in hand. The git auto-snapshot that
  captures the SAME change, though, stamps a generic structural subject
  ("auto-snapshot ... +2 ~3 -0 · memory"). This module lets a caller hand that
  already-computed WHY to the ONE sanctioned git writer (features/git-autosnapshot/
  auto-commit.sh) so `git log --oneline` reads as a why-changelog instead of noise.

CONTRACT (the one thing every caller does)
  drop("<verb>: <subject> [<detail>]")  ->  writes ONE line to $REPO/.dinomem-commit-reason
  The next auto-commit tick reads that line FIRST, uses it as the commit subject,
  then clears the file. Absent/stale -> the tick falls back to its structural
  subject. So this is a HINT, never a command: callers stay entirely git-free and
  nothing here ever touches git.

GOTCHAS (why it's built exactly this way)
  * ZERO LLM, ZERO git. The subject is an f-string over values the caller already
    has; adding a summarizer would pay to re-describe data in hand. Never do that.
  * Fail-open ALWAYS. A commit-subject hint is cosmetic; it must NEVER block, slow,
    or crash the memory write it decorates. Every failure path swallows and returns
    False — the caller ignores the return.
  * Single line, bounded. Newlines are stripped and the subject is truncated to
    SUBJECT_MAX so the git-log title stays a real title (full text lives in the diff).
  * Last-writer-wins by design. If two meaningful writes land inside one 15-min
    tick, the later reason overwrites — acceptable: both diffs are still captured;
    only the human-facing subject reflects the last why. (Multi-reason batching was
    considered and rejected as over-engineering for a cosmetic line.)
"""
import os
from pathlib import Path

SUBJECT_MAX = 72  # git-title discipline; detail beyond this stays in the diff

# Resolve $REPO the same way auto-commit.sh does: AUTOSNAP_REPO wins, else the
# dinomem workspace (sed-substituted at install), else a self-locate fallback.
def _repo() -> Path:
    r = os.environ.get("AUTOSNAP_REPO") or os.environ.get("DINOMEM_WORKSPACE")
    if r:
        return Path(r)
    # Fallback: two levels up from this file (procedures/ -> workspace root).
    return Path(__file__).resolve().parent.parent


def _hint_path() -> Path:
    # Must match auto-commit.sh's REASON_HINT default exactly.
    override = os.environ.get("AUTOSNAP_REASON_HINT")
    return Path(override) if override else (_repo() / ".dinomem-commit-reason")


def clean_subject(text: str) -> str:
    """One-line, whitespace-collapsed, bounded git-title. Pure; no I/O."""
    s = " ".join(str(text).split())  # collapse ALL whitespace incl newlines/tabs
    if len(s) > SUBJECT_MAX:
        s = s[: SUBJECT_MAX - 1].rstrip() + "\u2026"  # ellipsis marks truncation
    return s


def drop(reason: str) -> bool:
    """Hand the auto-snapshot writer a semantic subject for the next tick.

    Returns True if the hint was written, False on any failure or empty reason.
    Callers IGNORE the return (fail-open): a failed hint just means the next
    commit uses its structural subject.
    """
    try:
        subject = clean_subject(reason)
        if not subject:
            return False
        p = _hint_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(subject + "\n", encoding="utf-8")
        return True
    except Exception:
        return False  # cosmetic hint: never propagate


if __name__ == "__main__":
    import sys
    # tiny CLI so a shell caller (or a test) can drop a hint too:
    #   python3 commit_reason.py "resolve: note foo done_when met @abc123"
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        print("usage: commit_reason.py '<verb>: <subject> [<detail>]'", file=sys.stderr)
        sys.exit(2)
    ok = drop(" ".join(sys.argv[1:]))
    sys.exit(0 if ok else 1)
