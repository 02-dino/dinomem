#!/usr/bin/env python3
"""
git_history.py — git-backed history helper for dinomem (base-owned, shared).

WHY
  Several dinomem/neuron components independently hand-roll version control:
    - _memory_diff.py       : audit log of add/update/delete + before/after
    - resolve_done_notes.py : snapshot-before-delete recoverability
    - memory_retention.py   : age via filename (mtime unreliable)
  When the git-autosnapshot feature is active, git ALREADY captures all of this,
  byte-exact + timestamped + truly reversible. This helper exposes that git truth
  through a tiny stable API so those consumers can be BACKED by git instead of
  each reinventing it — while degrading cleanly to their old behavior when git is
  absent.

DESIGN CONSTRAINTS (dinomem philosophy)
  - stdlib ONLY (subprocess) — no new pip deps.
  - GIT-OPTIONAL / FAIL-OPEN: every public call returns a benign default (None /
    [] / False) if git is missing, the dir isn't a repo, or any git call errors.
    A caller can ALWAYS fall back to its non-git path. This helper NEVER raises to
    the caller and NEVER mutates the repo (all reads; the one write path,
    restore(), only ever `git checkout`s a path the caller explicitly asked for).
  - BOUNDED-HISTORY AWARE: the auto-snapshot retention collapses old snapshots
    (~30d default), so git history is reliable RECENT, not infinite. Callers that
    need older-than-window truth must keep their own long-term path (e.g. the
    distiller into permanent memory). Helpers here are recent-weighted by nature.
"""
import os
import subprocess
from datetime import datetime, timezone

# Per-call git timeout (seconds) so a wedged git can never hang a cron.
_GIT_TIMEOUT = float(os.environ.get("DINOMEM_GIT_TIMEOUT", "8") or "8")

# Cache repo-availability per top-level dir so we don't re-probe every call.
_repo_cache = {}


def _run(repo, args, timeout=None):
    """Run `git -C <repo> <args>`; return stdout str on success, else None.
    Fail-open: any error (missing git, non-repo, timeout, non-zero exit) -> None.
    """
    try:
        p = subprocess.run(
            ["git", "-C", str(repo)] + list(args),
            capture_output=True, text=True,
            timeout=timeout or _GIT_TIMEOUT,
        )
        if p.returncode != 0:
            return None
        return p.stdout
    except Exception:
        return None


def available(repo):
    """True if git exists AND <repo> is inside a git work tree. Cached."""
    key = str(repo)
    if key in _repo_cache:
        return _repo_cache[key]
    out = _run(repo, ["rev-parse", "--is-inside-work-tree"])
    ok = (out is not None and out.strip() == "true")
    _repo_cache[key] = ok
    return ok


def _repo_root(repo):
    """Absolute work-tree root, or None."""
    out = _run(repo, ["rev-parse", "--show-toplevel"])
    return out.strip() if out else None


# ─────────────────────────────────────────────────────────────────────────────
# Public API — every function is FAIL-OPEN (benign default on any failure) and
# READ-ONLY except restore(). Consumers use these as an ADDITIVE second source;
# they must keep their existing non-git behavior as the primary/fallback path.
# ─────────────────────────────────────────────────────────────────────────────

def file_first_seen(repo, path):
    """UTC datetime a file first entered git history, or None.
    Uses --follow so renames are traced. Recent-weighted: if history was
    collapsed by retention, this reflects the earliest RETAINED commit, not the
    true origin — callers needing absolute origin must not rely on this alone.
    """
    if not available(repo):
        return None
    out = _run(repo, ["log", "--follow", "--diff-filter=A", "--format=%ct",
                      "--", str(path)])
    if not out:
        # No add-commit found (e.g. collapsed baseline) -> fall back to oldest
        # commit touching the path at all.
        out = _run(repo, ["log", "--follow", "--format=%ct", "--", str(path)])
    if not out:
        return None
    epochs = [int(x) for x in out.split() if x.strip().isdigit()]
    if not epochs:
        return None
    return datetime.fromtimestamp(min(epochs), tz=timezone.utc)


def file_last_touched(repo, path):
    """UTC datetime a file was last modified in git history, or None."""
    if not available(repo):
        return None
    out = _run(repo, ["log", "-1", "--format=%ct", "--", str(path)])
    if not out or not out.strip().isdigit():
        return None
    return datetime.fromtimestamp(int(out.strip()), tz=timezone.utc)


def commit_count(repo, path):
    """How many commits touched a path (a recurrence/reinforcement signal).
    0 on any failure. Bounded by retention window.
    """
    if not available(repo):
        return 0
    out = _run(repo, ["rev-list", "--count", "HEAD", "--", str(path)])
    try:
        return int((out or "0").strip())
    except Exception:
        return 0


def content_at(repo, path, ref="HEAD"):
    """File content at a given ref (default last commit), or None.
    Useful as an ADDITIONAL recovery source (e.g. a note about to be deleted).
    """
    if not available(repo):
        return None
    return _run(repo, ["show", f"{ref}:./{path}"])


def diff_since(repo, ref="HEAD~1", pathspec=None):
    """Name-status diff (A/M/D + path) since ref, as a list of dicts.
    Returns [] on any failure. Cross-reference / enrichment ONLY — callers keep
    their own change-tracking as authoritative.
    """
    if not available(repo):
        return []
    args = ["diff", "--name-status", ref]
    if pathspec:
        args += ["--", str(pathspec)]
    out = _run(repo, args)
    if out is None:
        return []
    changes = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            code = parts[0].strip()[:1]  # A/M/D (or R# -> take first char)
            changes.append({"status": code, "path": parts[-1].strip()})
    return changes


def restore(repo, path, ref="HEAD"):
    """ADDITIONAL recovery option: restore a path from a ref via `git checkout`.
    The ONLY write path in this module, and only ever the caller-named path.
    Returns True on success, False otherwise. Callers must treat this as a
    supplementary recovery aid, never the sole safety mechanism.
    """
    if not available(repo):
        return False
    out = _run(repo, ["checkout", ref, "--", str(path)])
    return out is not None
