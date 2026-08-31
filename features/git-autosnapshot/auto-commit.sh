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

# -- SINGLE-INSTANCE GUARD (flock) ------------------------------------------
# WHY: without this a tick that HANGS (e.g. `git ls-files` stalling on a huge
# dirty tree — observed 15k+ files after a migration) keeps holding the git
# index, and the timer keeps spawning NEW ticks on top of it every interval.
# They stack, all wedge on the same held index, and the snapshot silently dies.
# A non-blocking flock makes a new tick EXIT immediately if one is already
# running, so ticks never pile up. Fail-open: if flock is absent (busybox/mac)
# or the lock dir isn't writable, just proceed (old behavior) rather than abort.
_RUN_LOCK="$GIT_DIR/.autosnap-run.lock"
if command -v flock >/dev/null 2>&1; then
  exec 9>"$_RUN_LOCK" 2>/dev/null || true
  if [ -e /proc/self/fd/9 ] && ! flock -n 9; then
    # Another tick holds it. Normal on a busy box -> quiet exit, not an error.
    exit 0
  fi
fi

# -- STALE index.lock GUARD -------------------------------------------------
# WHY: if a previous tick was SIGKILLed mid git-write (OOM / reboot / disk-full
# panic), git leaves a 0-byte $GIT_DIR/index.lock behind. Git then refuses ALL
# subsequent index ops forever, so the snapshot wedges permanently even after
# the original cause cleared. We ONLY hold the flock at this point (so no live
# sibling tick owns the lock), so an index.lock older than the stale threshold
# is provably orphaned -> remove it. Threshold guards against nuking a lock a
# concurrent NON-autosnap git op (unlikely on an isolated git-dir) just made.
_STALE_LOCK_AGE_S="${AUTOSNAP_STALE_LOCK_AGE_S:-120}"
if [ -f "$GIT_DIR/index.lock" ]; then
  _lk_age=$(( $(date +%s) - $(stat -c %Y "$GIT_DIR/index.lock" 2>/dev/null || echo 0) ))
  if [ "$_lk_age" -ge "$_STALE_LOCK_AGE_S" ]; then
    rm -f "$GIT_DIR/index.lock" 2>/dev/null || true
    echo "$(date '+%F %T') RECOVER: removed stale index.lock (age ${_lk_age}s)" >> "$REPO/logs/git-autosnapshot.log" 2>/dev/null || true
  fi
fi

# SEMANTIC commit-subject hint (two-tier subjects). A meaningful-write caller
# (memory_promote graduate/demote, valid_time supersede, resolve_done_notes
# resolve, extract_memory dedup-merge) drops the WHY of its change here as ONE
# line via scripts/lib/commit_reason.sh; this tick reads it FIRST, uses it as the
# commit subject, then clears it. Absent/stale/empty -> fall through to the
# structural auto-snapshot subject (a blind timer genuinely has no why). Keeps
# ALL git-writing in THIS one sanctioned script (callers stay git-free), zero new
# per-tick cost (the reason is a string the caller already held). Fail-open.
REASON_HINT="${AUTOSNAP_REASON_HINT:-$REPO/.dinomem-commit-reason}"
# A hint older than this is stale (its write already got committed / was missed).
REASON_MAX_AGE_S="${AUTOSNAP_REASON_MAX_AGE_S:-900}"

# Address git via the isolated git-dir + the repo as work-tree. Everything below
# uses `g` instead of bare `git`, so the user's own repo is never touched.
g() { git --git-dir="$GIT_DIR" --work-tree="$REPO" "$@"; }

# Housekeeping (gc/repack/lfs prune/retention) is background maintenance — it must
# NEVER compete with the live gateway. `gnice` runs the SAME git op at idle CPU
# (nice 19) + idle IO (ionice class 3) priority, so a heavy repack yields the
# machine instead of driving load to 12-20. nice/ionice are optional: if absent
# (busybox/mac), fall through to plain `g` so behavior is preserved.
_NICE=""; command -v nice   >/dev/null 2>&1 && _NICE="nice -n 19"
_IONICE=""; command -v ionice >/dev/null 2>&1 && _IONICE="ionice -c3"
gnice() { $_NICE $_IONICE git --git-dir="$GIT_DIR" --work-tree="$REPO" "$@"; }
# git-lfs prune resolves HEAD from the CWD, not from --git-dir/--work-tree flags,
# so calling it via gnice() fails with "Git can't resolve ref HEAD" and leaks
# orphaned LFS objects forever (observed: ~1GB of stale blobs never pruned despite
# the timer running). Run lfs FROM INSIDE the work-tree with GIT_DIR via env so
# HEAD resolves. Still niced/ioniced; still fail-open.
lfsnice() { ( cd "$REPO" && GIT_DIR="$GIT_DIR" GIT_WORK_TREE="$REPO" $_NICE $_IONICE git lfs "$@"; ); }

# Aggressive gc (full delta recompute) is expensive and near-useless to repeat:
# once the pack is optimal, re-running it every ≥90% tick burns CPU for nothing.
# Gate it to at most once per AGGR_GC_COOLDOWN_H hours via a stamp file.
AGGR_GC_COOLDOWN_H="${AUTOSNAP_AGGR_GC_COOLDOWN_H:-24}"
_AGGR_STAMP="$GIT_DIR/.last-aggressive-gc"
aggr_gc_due() {
  [ -f "$_AGGR_STAMP" ] || return 0
  local last now
  last=$(cat "$_AGGR_STAMP" 2>/dev/null || echo 0)
  now=$(date +%s)
  [ $(( now - last )) -ge $(( AGGR_GC_COOLDOWN_H * 3600 )) ]
}

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
  done < <(_to g ls-files --others --exclude-standard 2>/dev/null)

  # -- Stage everything not ignored, minus oversized new files ---------------
  # WHY the _to timeout wrap: on a huge dirty tree these scans can stall for
  # minutes (observed hang in `ls-files` at pipe_write). A bounded timeout makes
  # a wedged tick DIE and release the flock/index instead of hanging forever;
  # the next tick retries clean. Fail-open: `timeout` absent -> plain g.
  _to g add -A -- . "${EXCLUDES[@]}" 2>/dev/null || _to g add -A 2>/dev/null || true

  # After excludes/ignores there may be nothing actually staged -> no empty commit.
  if ! g diff --cached --quiet 2>/dev/null; then
    STAMP="$(date '+%Y-%m-%d %H:%M:%S %Z')"
    N="$(g diff --cached --name-only | wc -l | tr -d ' ')"

    # -- TIER 1: SEMANTIC subject from a meaningful-write hint (zero LLM) -------
    # A caller that KNEW why it wrote (promotion/supersede/resolve/dedup) dropped
    # the WHY into $REASON_HINT. Use it as the subject when present + fresh, then
    # CLEAR it so a later blind tick doesn't reuse a stale why. The reason is a
    # string the caller already held -> no new cost, no model, no git in callers.
    SUBJ=""
    if [ -s "$REASON_HINT" ]; then
      _hint_age=$(( $(date +%s) - $(stat -c %Y "$REASON_HINT" 2>/dev/null || echo 0) ))
      if [ "$_hint_age" -ge 0 ] && [ "$_hint_age" -le "$REASON_MAX_AGE_S" ]; then
        SUBJ="$(head -1 "$REASON_HINT" | tr -d '\r' | cut -c1-72)"
      fi
      rm -f "$REASON_HINT" 2>/dev/null || true   # consume-once, even if stale
    fi

    # -- TIER 2: STRUCTURAL subject (pure git, zero LLM) — the fallback --------
    # A blind timer genuinely has no why. A machine-recoverable diff already
    # exists; this line just makes `git log --oneline` scannable for RECOVERY
    # TRIAGE: how many added/modified/deleted and which top-level dir dominated.
    # No semantic summary (would cost a cheap-model call per 15-min tick for a
    # log almost nobody reads); the real "what changed" stays in the diff itself.
    if [ -z "$SUBJ" ]; then
      ADDED=$(g diff --cached --name-status | grep -c '^A' || true)
      MODED=$(g diff --cached --name-status | grep -c '^M' || true)
      DELED=$(g diff --cached --name-status | grep -c '^D' || true)
      # Dominant top-level dir among staged paths (e.g. 'memory', 'logs').
      TOPDIR=$(g diff --cached --name-only \
        | sed -e 's#^\([^/]*\)/.*#\1#' -e 's#^[^/]*$#(root)#' \
        | sort | uniq -c | sort -rn | head -1 | awk '{print $2}')
      [ -z "$TOPDIR" ] && TOPDIR='(root)'
      SUBJ="auto-snapshot ${STAMP} · +${ADDED} ~${MODED} -${DELED} · ${TOPDIR} (${N} file(s))"
    fi

    g commit --quiet -m "$SUBJ" 2>/dev/null \
      || g commit --quiet -m "auto-snapshot ${STAMP} (${N} file(s))" 2>/dev/null \
      || true
  fi
fi

# ── DISK-AWARE housekeeping ──────────────────────────────────────────────────
# Escalate by how full the filesystem holding the repo actually is.
DISK_PCT=$(df --output=pcent "$REPO" 2>/dev/null | tail -1 | tr -dc '0-9')
[ -z "$DISK_PCT" ] && DISK_PCT=0

# All housekeeping git ops below run via `gnice` (idle CPU+IO) so they never
# drive gateway-competing load. Aggressive gc is additionally cooldown-gated.
if [ "$DISK_PCT" -ge 90 ]; then
  gnice reflog expire --expire=now --all 2>/dev/null || true
  if aggr_gc_due; then
    echo "$(date '+%F %T') EMERGENCY disk=${DISK_PCT}% -> aggressive gc + full lfs prune + 7d retention" >> "$LOG"
    gnice gc --quiet --prune=now --aggressive 2>/dev/null || true
    date +%s > "$_AGGR_STAMP" 2>/dev/null || true
  else
    # Aggressive repack ran within the cooldown window; do the cheap prune only
    # so we still reclaim space each tick without re-paying the full repack.
    echo "$(date '+%F %T') EMERGENCY disk=${DISK_PCT}% -> light gc prune=now (aggressive on cooldown) + full lfs prune + 7d retention" >> "$LOG"
    gnice gc --quiet --prune=now 2>/dev/null || true
  fi
  command -v git-lfs >/dev/null 2>&1 && lfsnice prune --force 2>/dev/null || true
  AUTOSNAP_RETAIN_DAYS=7 AUTOSNAP_REPO="$REPO" AUTOSNAP_GIT_DIR="$GIT_DIR" AUTOSNAP_BRANCH="$BRANCH" \
    $_NICE $_IONICE bash "$SELF_DIR/git-retention.sh" 2>/dev/null || true
elif [ "$DISK_PCT" -ge 80 ]; then
  echo "$(date '+%F %T') WARN disk=${DISK_PCT}% -> gc prune=now + lfs prune + ${RETAIN_DAYS}d retention" >> "$LOG"
  gnice gc --quiet --prune=now 2>/dev/null || true
  command -v git-lfs >/dev/null 2>&1 && lfsnice prune 2>/dev/null || true
  AUTOSNAP_RETAIN_DAYS="$RETAIN_DAYS" AUTOSNAP_REPO="$REPO" AUTOSNAP_GIT_DIR="$GIT_DIR" AUTOSNAP_BRANCH="$BRANCH" \
    $_NICE $_IONICE bash "$SELF_DIR/git-retention.sh" 2>/dev/null || true
else
  # HEALTHY (<80%): light housekeeping ~hourly (the :00-:14 tick only).
  if [ "$(( $(date +%-M) / 15 ))" -eq 0 ]; then
    gnice gc --quiet --auto 2>/dev/null || true
    command -v git-lfs >/dev/null 2>&1 && lfsnice prune 2>/dev/null || true
  fi
fi
