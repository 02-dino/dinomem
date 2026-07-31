#!/usr/bin/env bash
# dinomem git-autosnapshot — periodic local git snapshot of an OpenClaw repo.
#
# Commits ALL non-ignored changes (tracked mods/deletions AND brand-new files)
# on a timer, so your work is always recoverable. Local-only: no remote, nothing
# leaves the box. Purely a rollback-safety net on top of your real commits.
#
# CONFIG (all overridable via env; installer bakes REPO in):
#   AUTOSNAP_REPO      repo root to snapshot        (required; no sane default)
#   AUTOSNAP_MAX_MB    per-file ceiling for NEW files that get auto-added (default 10)
#   AUTOSNAP_RETAIN_DAYS  granular-history window before old snapshots collapse (30)
#   AUTOSNAP_BRANCH    branch to snapshot           (default: current branch)
#
# A size guard refuses to auto-add any single NEW file larger than MAX_MB, so a
# stray model/image/video dump can never bloat .git. Disk-aware housekeeping
# (gc / lfs prune / history retention) escalates as the disk fills.
set -euo pipefail

REPO="${AUTOSNAP_REPO:-}"
MAX_MB="${AUTOSNAP_MAX_MB:-10}"
RETAIN_DAYS="${AUTOSNAP_RETAIN_DAYS:-30}"
[ -z "$REPO" ] && { echo "auto-commit: AUTOSNAP_REPO not set" >&2; exit 2; }
[ -d "$REPO/.git" ] || { echo "auto-commit: $REPO is not a git repo" >&2; exit 2; }
cd "$REPO"

BRANCH="${AUTOSNAP_BRANCH:-$(git symbolic-ref --short -q HEAD || echo main)}"
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="$REPO/logs/git-autosnapshot.log"
mkdir -p "$REPO/logs" 2>/dev/null || true

# ── Size guard: exclude oversized NEW files from this run (stay on disk) ──────
EXCLUDES=()
while IFS= read -r f; do
  [ -z "$f" ] && continue
  sz=$(stat -c '%s' "$f" 2>/dev/null || echo 0)
  if [ "$sz" -gt $((MAX_MB*1024*1024)) ]; then
    EXCLUDES+=(":(exclude)$f")
  fi
done < <(git ls-files --others --exclude-standard 2>/dev/null)

# ── Stage everything not ignored, minus oversized new files ──────────────────
git add -A -- . "${EXCLUDES[@]}" 2>/dev/null || git add -A 2>/dev/null || true

# Nothing staged? exit quietly (no empty commits).
if git diff --cached --quiet 2>/dev/null; then
  # still run housekeeping below so cleanup happens even on idle ticks
  :
else
  STAMP="$(date '+%Y-%m-%d %H:%M:%S %Z')"
  N="$(git diff --cached --name-only | wc -l | tr -d ' ')"
  git commit --quiet -m "auto-snapshot ${STAMP} (${N} file(s))" 2>/dev/null || true
fi

# ── DISK-AWARE housekeeping ──────────────────────────────────────────────────
# Escalate by how full the filesystem holding the repo actually is.
DISK_PCT=$(df --output=pcent "$REPO" 2>/dev/null | tail -1 | tr -dc '0-9')
[ -z "$DISK_PCT" ] && DISK_PCT=0

if [ "$DISK_PCT" -ge 90 ]; then
  echo "$(date '+%F %T') EMERGENCY disk=${DISK_PCT}% -> aggressive gc + full lfs prune + 7d retention" >> "$LOG"
  git reflog expire --expire=now --all 2>/dev/null || true
  git gc --quiet --prune=now --aggressive 2>/dev/null || true
  command -v git-lfs >/dev/null 2>&1 && git lfs prune --force --quiet 2>/dev/null || true
  AUTOSNAP_RETAIN_DAYS=7 AUTOSNAP_REPO="$REPO" AUTOSNAP_BRANCH="$BRANCH" \
    bash "$SELF_DIR/git-retention.sh" 2>/dev/null || true
elif [ "$DISK_PCT" -ge 80 ]; then
  echo "$(date '+%F %T') WARN disk=${DISK_PCT}% -> gc prune=now + lfs prune + ${RETAIN_DAYS}d retention" >> "$LOG"
  git gc --quiet --prune=now 2>/dev/null || true
  command -v git-lfs >/dev/null 2>&1 && git lfs prune --quiet 2>/dev/null || true
  AUTOSNAP_RETAIN_DAYS="$RETAIN_DAYS" AUTOSNAP_REPO="$REPO" AUTOSNAP_BRANCH="$BRANCH" \
    bash "$SELF_DIR/git-retention.sh" 2>/dev/null || true
else
  # HEALTHY (<80%): light housekeeping ~hourly (the :00-:14 tick only).
  if [ "$(( $(date +%-M) / 15 ))" -eq 0 ]; then
    git gc --quiet --auto 2>/dev/null || true
    command -v git-lfs >/dev/null 2>&1 && git lfs prune --quiet 2>/dev/null || true
  fi
fi
