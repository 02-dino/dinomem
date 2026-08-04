#!/usr/bin/env bash
# dinomem git-autosnapshot — periodic local git snapshot of an OpenClaw repo.
#
# Commits ALL non-ignored changes (tracked mods/deletions AND brand-new files)
# on a timer, so your work is always recoverable. Local-only: no remote, nothing
# leaves the box. Purely a rollback-safety net on top of your real commits.
#
# ISOLATION (why this never touches your own repo):
#   The snapshot object DB lives in a SEPARATE git-dir INSIDE the repo
#   (default: $REPO/.dinomem-snap.git) addressed via --git-dir/--work-tree.
#   Your own $REPO/.git (if any) is never read or written. Ignore rules live in
#   the snapshot git-dir's own info/exclude, so no .gitignore is dropped into
#   your working tree.
#
# CONFIG (all overridable via env; installer bakes REPO in):
#   AUTOSNAP_REPO      repo root (work-tree) to snapshot   (required)
#   AUTOSNAP_GIT_DIR   snapshot git-dir  (default: $REPO/.dinomem-snap.git)
#   AUTOSNAP_MAX_MB    per-file ceiling for NEW files that get auto-added (default 10)
#   AUTOSNAP_RETAIN_DAYS  granular-history window before old snapshots collapse (30)
#   AUTOSNAP_BRANCH    branch to snapshot           (default: current branch)
#
# A size guard refuses to auto-add any single NEW file larger than MAX_MB, so a
# stray model/data dump can never bloat the snapshot DB. LFS-tracked paths
# (media/archives/pdf per gitattributes) are EXEMPT from the size guard: LFS
# stores their bytes outside history, so a 40MB .mp4 is added via LFS instead of
# being dropped. Only oversized NON-LFS blobs (e.g. .jsonl/.sqlite dumps) are
# excluded. Disk-aware housekeeping (gc / lfs prune / history retention)
# escalates as the disk fills.
set -euo pipefail

REPO="${AUTOSNAP_REPO:-}"
MAX_MB="${AUTOSNAP_MAX_MB:-10}"
RETAIN_DAYS="${AUTOSNAP_RETAIN_DAYS:-30}"
[ -z "$REPO" ] && { echo "auto-commit: AUTOSNAP_REPO not set" >&2; exit 2; }

# Isolated snapshot git-dir (NOT the user's $REPO/.git).
GIT_DIR="${AUTOSNAP_GIT_DIR:-$REPO/.dinomem-snap.git}"
[ -f "$GIT_DIR/HEAD" ] || { echo "auto-commit: snapshot git-dir not initialized at $GIT_DIR (run install.sh)" >&2; exit 2; }

# Address git via the isolated git-dir + the repo as work-tree. Everything below
# uses `g` instead of bare `git`, so the user's own repo is never touched.
g() { git --git-dir="$GIT_DIR" --work-tree="$REPO" "$@"; }

BRANCH="${AUTOSNAP_BRANCH:-$(g symbolic-ref --short -q HEAD || echo main)}"
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="$REPO/logs/git-autosnapshot.log"
mkdir -p "$REPO/logs" 2>/dev/null || true

# -- CHEAP IDLE SHORT-CIRCUIT (the efficiency gate) --------------------------
# Most 15-min ticks have NOTHING to commit. `git status --porcelain` is fast
# (backed by the fsmonitor + untracked-cache enabled at install), so probe it
# FIRST; when the tree is clean, skip ALL the expensive work (ls-files scan,
# add -A over the whole tree, commit). This makes an idle tick genuinely cheap
# and prevents redundant snapshots. Housekeeping below stays gated too.
if [ -n "$(g status --porcelain 2>/dev/null | head -c1)" ]; then

  # -- Size guard: exclude oversized NEW files from this run (stay on disk) ---
  # Allowlist: an optional `.dinomem-keep-large` at the repo root lets the user
  # opt specific oversized NON-LFS blobs (irreproducible dumps you explicitly
  # want versioned) past the guard. One glob per line; blank lines and lines
  # starting with # are ignored. Globs are matched against the repo-relative
  # path (e.g. `data/snapshot-*.jsonl`, `exports/*.sqlite`). Default: absent =
  # nothing allowlisted = original safe behavior.
  KEEP_GLOBS=()
  if [ -f "$REPO/.dinomem-keep-large" ]; then
    while IFS= read -r line || [ -n "$line" ]; do
      line="${line%%$'\r'}"                    # strip CR (CRLF files)
      case "$line" in ''|'#'*) continue ;; esac  # skip blank / comment
      KEEP_GLOBS+=("$line")
    done < "$REPO/.dinomem-keep-large"
  fi
  # keep_large <relpath> -> 0 if it matches any allowlist glob, else 1
  keep_large() {
    local p="$1" glob
    for glob in "${KEEP_GLOBS[@]:-}"; do
      [ -z "$glob" ] && continue
      # shellcheck disable=SC2254  # glob is intentionally a pattern here
      case "$p" in $glob) return 0 ;; esac
    done
    return 1
  }
  EXCLUDES=()
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    sz=$(stat -c '%s' "$REPO/$f" 2>/dev/null || echo 0)
    if [ "$sz" -gt $((MAX_MB*1024*1024)) ]; then
      # LFS-aware: if this path is LFS-tracked, its bytes live OUTSIDE history,
      # so size is irrelevant -> let it through. Only exclude oversized non-LFS
      # blobs. `check-attr filter` returns 'lfs' when a gitattributes rule matches.
      if g check-attr filter -- "$f" 2>/dev/null | grep -q ': filter: lfs$'; then
        : # LFS-tracked oversized file -> keep (stored via LFS, snapshot stays tiny)
      elif keep_large "$f"; then
        : # user-allowlisted oversized non-LFS blob -> keep (opted in explicitly)
      else
        EXCLUDES+=(":(exclude)$f")
      fi
    fi
  done < <(g ls-files --others --exclude-standard 2>/dev/null)

  # -- Stage everything not ignored, minus oversized new files ---------------
  g add -A -- . "${EXCLUDES[@]}" 2>/dev/null || g add -A 2>/dev/null || true

  # After excludes/ignores there may be nothing actually staged -> no empty commit.
  if ! g diff --cached --quiet 2>/dev/null; then
    STAMP="$(date '+%Y-%m-%d %H:%M:%S %Z')"
    N="$(g diff --cached --name-only | wc -l | tr -d ' ')"

    # -- Structural subject (pure git, zero LLM) -------------------------------
    # A machine-recoverable diff already exists; this line just makes `git log
    # --oneline` scannable for RECOVERY TRIAGE: how many added/modified/deleted
    # and which top-level dir dominated. No semantic summary (would cost a
    # cheap-model call per 15-min tick for a log almost nobody reads); the real
    # "what changed" stays in the diff itself.
    ADDED=$(g diff --cached --name-status | grep -c '^A' || true)
    MODED=$(g diff --cached --name-status | grep -c '^M' || true)
    DELED=$(g diff --cached --name-status | grep -c '^D' || true)
    # Dominant top-level dir among staged paths (e.g. 'memory', 'logs').
    TOPDIR=$(g diff --cached --name-only \
      | sed -e 's#^\([^/]*\)/.*#\1#' -e 's#^[^/]*$#(root)#' \
      | sort | uniq -c | sort -rn | head -1 | awk '{print $2}')
    [ -z "$TOPDIR" ] && TOPDIR='(root)'
    SUBJ="auto-snapshot ${STAMP} · +${ADDED} ~${MODED} -${DELED} · ${TOPDIR} (${N} file(s))"

    g commit --quiet -m "$SUBJ" 2>/dev/null \
      || g commit --quiet -m "auto-snapshot ${STAMP} (${N} file(s))" 2>/dev/null \
      || true
  fi
fi

# ── DISK-AWARE housekeeping ──────────────────────────────────────────────────
# Escalate by how full the filesystem holding the repo actually is.
DISK_PCT=$(df --output=pcent "$REPO" 2>/dev/null | tail -1 | tr -dc '0-9')
[ -z "$DISK_PCT" ] && DISK_PCT=0

if [ "$DISK_PCT" -ge 90 ]; then
  echo "$(date '+%F %T') EMERGENCY disk=${DISK_PCT}% -> aggressive gc + full lfs prune + 7d retention" >> "$LOG"
  g reflog expire --expire=now --all 2>/dev/null || true
  g gc --quiet --prune=now --aggressive 2>/dev/null || true
  command -v git-lfs >/dev/null 2>&1 && g lfs prune --force --quiet 2>/dev/null || true
  AUTOSNAP_RETAIN_DAYS=7 AUTOSNAP_REPO="$REPO" AUTOSNAP_GIT_DIR="$GIT_DIR" AUTOSNAP_BRANCH="$BRANCH" \
    bash "$SELF_DIR/git-retention.sh" 2>/dev/null || true
elif [ "$DISK_PCT" -ge 80 ]; then
  echo "$(date '+%F %T') WARN disk=${DISK_PCT}% -> gc prune=now + lfs prune + ${RETAIN_DAYS}d retention" >> "$LOG"
  g gc --quiet --prune=now 2>/dev/null || true
  command -v git-lfs >/dev/null 2>&1 && g lfs prune --quiet 2>/dev/null || true
  AUTOSNAP_RETAIN_DAYS="$RETAIN_DAYS" AUTOSNAP_REPO="$REPO" AUTOSNAP_GIT_DIR="$GIT_DIR" AUTOSNAP_BRANCH="$BRANCH" \
    bash "$SELF_DIR/git-retention.sh" 2>/dev/null || true
else
  # HEALTHY (<80%): light housekeeping ~hourly (the :00-:14 tick only).
  if [ "$(( $(date +%-M) / 15 ))" -eq 0 ]; then
    g gc --quiet --auto 2>/dev/null || true
    command -v git-lfs >/dev/null 2>&1 && g lfs prune --quiet 2>/dev/null || true
  fi
fi
