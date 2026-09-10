#!/usr/bin/env bash
# dinomem git-autosnapshot — rebuild-store.sh
#
# LAST RESORT for a pathologically bloated .dinomem-snap.git that in-place gc
# can't shrink (e.g. a leaked vector_db/sqlite history from before the store's
# ignore patterns covered it — the history stays even after the files are
# untracked; only a rebuild drops it). Normal operation should NEVER need
# this: auto-commit.sh's disk-aware gc/prune/retention path handles ordinary
# growth. Reach for this only when a store is tens of GB and gc --aggressive
# on it would itself risk OOM (observed 2026-09-09: exactly that, on a 34G
# store, is what forced this rebuild path into existence).
#
# WHAT IT DOES: moves the current store aside, inits a fresh one, commits a
# single baseline snapshot of the current work-tree (all in-history bloat
# dropped — only the LATEST state survives, no undo-history), low-mem gc's
# it, then VERIFIES the new store has a real HEAD before removing the old
# one. If verification fails, the old store is left in place (not deleted)
# and rebuild aborts non-zero — never silently double-loses data.
#
# ORPHAN SAFETY: if this process is killed mid-rebuild (OOM/reboot/disk-full)
# after the mv but before final cleanup, the old store is left parked as
# "$GIT_DIR.OLD-<timestamp>". auto-commit.sh's per-tick orphan sweep (see
# "STALE REBUILD-ORPHAN SWEEP" in that script) will reclaim it automatically
# after AUTOSNAP_ORPHAN_AGE_MIN (default 30min) — this is what was MISSING
# 2026-09-09 and let a dead rebuild's 34G leftover sit until disk hit 0 bytes.
#
# Usage:
#   bash rebuild-store.sh --repo DIR [--git-dir DIR] [--keep-old]
#
# Options:
#   --repo DIR      work-tree whose .dinomem-snap.git gets rebuilt (required)
#   --git-dir DIR   snapshot git-dir (default: $REPO/.dinomem-snap.git)
#   --keep-old      don't delete the verified-safe old store; still get swept
#                    later by auto-commit.sh's orphan sweep unless you rename
#                    it out of the .OLD-*/.old-* pattern yourself
set -euo pipefail

REPO=""
GIT_DIR=""
KEEP_OLD=0
while [ $# -gt 0 ]; do
  case "$1" in
    --repo)     REPO="$2"; shift 2 ;;
    --git-dir)  GIT_DIR="$2"; shift 2 ;;
    --keep-old) KEEP_OLD=1; shift ;;
    -h|--help)  grep -E '^#( |$)' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
[ -z "$REPO" ] && { echo "rebuild-store: --repo required" >&2; exit 2; }
REPO="$(cd "$REPO" && pwd)"
[ -z "$GIT_DIR" ] && GIT_DIR="$REPO/.dinomem-snap.git"
[ -f "$GIT_DIR/HEAD" ] || { echo "rebuild-store: no store at $GIT_DIR" >&2; exit 2; }

# Same low-mem knobs as auto-commit.sh's gnice() — a bloated store is exactly
# the case where an unbounded gc/repack would OOM (the original incident).
PACK_WINDOW_MEM="${AUTOSNAP_PACK_WINDOW_MEM:-64m}"
PACK_THREADS="${AUTOSNAP_PACK_THREADS:-1}"

before=$(du -sh "$GIT_DIR" 2>/dev/null | cut -f1)
echo "=== rebuild $GIT_DIR (before: $before) ==="

OLD="$GIT_DIR.OLD-$(date +%Y%m%dT%H%M%S)"
mv "$GIT_DIR" "$OLD"
git --git-dir="$GIT_DIR" --work-tree="$REPO" init -q
git --git-dir="$GIT_DIR" config core.worktree "$REPO" 2>/dev/null || true
git --git-dir="$GIT_DIR" config core.bare false 2>/dev/null || true
# Carry over the private ignore/attribute rules (not history) from the old
# store so the fresh one doesn't immediately re-leak whatever the old one
# was already excluding.
[ -f "$OLD/info/exclude" ] && { mkdir -p "$GIT_DIR/info"; cp "$OLD/info/exclude" "$GIT_DIR/info/exclude"; }
[ -f "$OLD/info/attributes" ] && cp "$OLD/info/attributes" "$GIT_DIR/info/attributes"

git --git-dir="$GIT_DIR" --work-tree="$REPO" add -A
git --git-dir="$GIT_DIR" --work-tree="$REPO" \
  -c user.name=dinomem -c user.email=dinomem@local \
  commit -q -m "rebuild: fresh baseline (old history dropped, see $OLD)"
git -c "pack.windowMemory=$PACK_WINDOW_MEM" -c "pack.threads=$PACK_THREADS" \
  --git-dir="$GIT_DIR" gc --quiet --prune=now

# VERIFY before touching the old store — never delete on a hunch.
if ! git --git-dir="$GIT_DIR" rev-parse -q --verify HEAD >/dev/null 2>&1; then
  echo "rebuild-store: FAILED — new store has no HEAD, old store left at $OLD (nothing deleted)" >&2
  exit 1
fi

after=$(du -sh "$GIT_DIR" 2>/dev/null | cut -f1)
echo "=== rebuild done (after: $after) ==="
if [ "$KEEP_OLD" = 1 ]; then
  echo "old store kept at: $OLD (auto-commit.sh's orphan sweep will reclaim it after AUTOSNAP_ORPHAN_AGE_MIN unless you rename it)"
else
  rm -rf "$OLD"
  echo "old store deleted (verified new HEAD first)"
fi
