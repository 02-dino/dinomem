#!/usr/bin/env bash
# dinomem git-autosnapshot — history retention.
#
# Bounds .git growth from timer-driven auto-snapshots by collapsing CONSECUTIVE
# runs of "auto-snapshot ..." commits OLDER than RETAIN_DAYS into a single
# baseline commit. Recent snapshots (< RETAIN_DAYS) stay fully granular.
#
# NEVER touches: any commit whose subject is NOT "auto-snapshot ..." (your
# hand-written, meaningful commits stay permanent at ANY age), and never your
# working tree / files (only history metadata is rewritten; the tip tree is
# byte-identical before and after).
#
# CONFIG (env):
#   AUTOSNAP_REPO         repo root (work-tree)    (required)
#   AUTOSNAP_GIT_DIR      snapshot git-dir         (default: $REPO/.dinomem-snap.git)
#   AUTOSNAP_RETAIN_DAYS  keep-granular window     (default 30)
#   AUTOSNAP_BRANCH       branch                   (default: current)
#   AUTOSNAP_MIN_COLLAPSE minimum old snapshots to bother collapsing (default 50)
#
# SAFETY: backs up the branch tip to refs/backup/retention-<ts> before rewriting;
# on any rebase failure it aborts and restores the tip. Bails on a dirty tree.
set -euo pipefail

REPO="${AUTOSNAP_REPO:-}"
RETAIN_DAYS="${AUTOSNAP_RETAIN_DAYS:-30}"
MIN_COLLAPSE="${AUTOSNAP_MIN_COLLAPSE:-50}"
[ -z "$REPO" ] && { echo "git-retention: AUTOSNAP_REPO not set" >&2; exit 2; }

# Isolated snapshot git-dir (NOT the user's $REPO/.git).
GIT_DIR="${AUTOSNAP_GIT_DIR:-$REPO/.dinomem-snap.git}"
[ -f "$GIT_DIR/HEAD" ] || { echo "git-retention: snapshot git-dir not initialized at $GIT_DIR" >&2; exit 2; }

# All git calls go through the isolated git-dir + repo work-tree.
g() { git --git-dir="$GIT_DIR" --work-tree="$REPO" "$@"; }

BRANCH="${AUTOSNAP_BRANCH:-$(g symbolic-ref --short -q HEAD || echo main)}"
LOG="$REPO/logs/git-autosnapshot.log"
mkdir -p "$REPO/logs" 2>/dev/null || true

# Never run mid-operation or on a dirty index that could confuse the rewrite.
if ! g diff --quiet 2>/dev/null || ! g diff --cached --quiet 2>/dev/null; then
  echo "$(date '+%F %T') RETENTION skip: working tree dirty" >> "$LOG"
  exit 0
fi

CUTOFF_EPOCH=$(date -d "-${RETAIN_DAYS} days" +%s 2>/dev/null || date -v-"${RETAIN_DAYS}"d +%s)

# Find the OLDEST commit that must be RETAINED: newer than cutoff, OR a
# non-auto-snapshot (meaningful) commit. Everything strictly older AND
# auto-snapshot-only gets collapsed under a single baseline.
BOUNDARY=""
while IFS='|' read -r sha epoch subj; do
  keep=0
  [ "$epoch" -ge "$CUTOFF_EPOCH" ] && keep=1
  case "$subj" in
    auto-snapshot*) : ;;      # collapsible IF also old
    *) keep=1 ;;              # meaningful commit -> always keep
  esac
  if [ "$keep" -eq 1 ]; then
    BOUNDARY="$sha"
    break
  fi
done < <(g log --reverse --format='%H|%ct|%s' "$BRANCH")

# Nothing to collapse (all commits recent or meaningful).
[ -z "$BOUNDARY" ] && exit 0

# Parent of BOUNDARY = last commit to squash. If BOUNDARY is root, nothing before it.
if ! PARENT=$(g rev-parse --verify "${BOUNDARY}^" 2>/dev/null); then
  exit 0
fi

# Count collapsible commits; skip if trivially small.
NUM=$(g rev-list --count "$PARENT" 2>/dev/null || echo 0)
if [ "$NUM" -lt "$MIN_COLLAPSE" ]; then
  exit 0
fi

# Backup branch tip before rewriting.
BK="refs/backup/retention-$(date +%Y%m%d-%H%M%S)"
g update-ref "$BK" "$BRANCH"

# Baseline = orphan commit with PARENT's tree (exact file state), no history.
PARENT_TREE=$(g rev-parse "${PARENT}^{tree}")
BASELINE=$(g commit-tree "$PARENT_TREE" -m "baseline: collapsed ${NUM} auto-snapshots older than ${RETAIN_DAYS}d (files preserved; backup ${BK})")

# Replay BOUNDARY..BRANCH onto the new baseline.
if g rebase --onto "$BASELINE" "$PARENT" "$BRANCH" >/dev/null 2>&1; then
  echo "$(date '+%F %T') RETENTION ok: collapsed ${NUM} old auto-snapshots -> baseline ${BASELINE:0:8} (backup ${BK})" >> "$LOG"
  g reflog expire --expire=now --all 2>/dev/null || true
  g gc --quiet --prune=now 2>/dev/null || true
else
  g rebase --abort 2>/dev/null || true
  g update-ref "$BRANCH" "$BK"
  echo "$(date '+%F %T') RETENTION FAILED: rebase aborted, restored ${BRANCH} from ${BK}" >> "$LOG"
  exit 1
fi
