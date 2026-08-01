#!/usr/bin/env bash
# dinomem git-autosnapshot — installer.
# Sets up a timer that auto-snapshots an OpenClaw git repo every N minutes.
# Idempotent. Prefers systemd timer; falls back to cron when systemd is absent.
#
# Usage:
#   bash install.sh [--repo DIR] [--interval-min N] [--max-mb N]
#                   [--retain-days N] [--no-lfs] [--force] [--dry-run] [--uninstall]
#
# Options:
#   --repo DIR        git repo to snapshot   (default: $OPENCLAW_HOME or ~/.openclaw)
#   --interval-min N  snapshot interval      (default: 15)
#   --max-mb N        per-file ceiling for auto-added NEW files (default: 10)
#   --retain-days N   granular-history window before old snapshots collapse (default: 30)
#   --no-lfs          skip git-lfs media tracking setup
#   --force           overwrite existing units/scripts
#   --dry-run         preview only, write nothing
#   --uninstall       remove timer/cron + units (keeps your commits & scripts)
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${OPENCLAW_HOME:-$HOME/.openclaw}"
GIT_DIR=""          # isolated snapshot git-dir; default set after REPO resolves
INTERVAL_MIN=15
MAX_MB=10
RETAIN_DAYS=30
DO_LFS=1
FORCE=0
DRY_RUN=0
UNINSTALL=0

while [ $# -gt 0 ]; do
  case "$1" in
    --repo)         REPO="$2"; shift 2 ;;
    --git-dir)      GIT_DIR="$2"; shift 2 ;;
    --interval-min) INTERVAL_MIN="$2"; shift 2 ;;
    --max-mb)       MAX_MB="$2"; shift 2 ;;
    --retain-days)  RETAIN_DAYS="$2"; shift 2 ;;
    --no-lfs)       DO_LFS=0; shift ;;
    --force)        FORCE=1; shift ;;
    --dry-run)      DRY_RUN=1; shift ;;
    --uninstall)    UNINSTALL=1; shift ;;
    -h|--help)      grep -E '^#( |$)' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

ok()   { printf '  \033[32m[ok]\033[0m   %s\n' "$*"; }
skip() { printf '  \033[33m[skip]\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m[warn]\033[0m %s\n' "$*"; }
fail() { printf '  \033[31m[fail]\033[0m %s\n' "$*"; exit 1; }
hr()   { printf '\033[1m== %s ==\033[0m\n' "$*"; }
plan() { printf '  \033[36m[plan]\033[0m %s\n' "$*"; }

REPO="$(cd "$REPO" 2>/dev/null && pwd || echo "$REPO")"
# Isolated snapshot git-dir INSIDE the repo, but SEPARATE from any user .git.
# Files stay in place; only the object DB lives here. Never collides with the
# user's own $REPO/.git (which we never read or write).
[ -z "$GIT_DIR" ] && GIT_DIR="$REPO/.dinomem-snap.git"
# git addressing helper: isolated git-dir + repo as work-tree.
g() { git --git-dir="$GIT_DIR" --work-tree="$REPO" "$@"; }
UNIT_BASE="dinomem-autosnapshot"
# Distinct unit name per repo so multiple repos don't collide.
REPO_TAG="$(echo "$REPO" | tr -c 'a-zA-Z0-9' '-' | sed 's/--*/-/g;s/^-//;s/-$//')"
SVC="${UNIT_BASE}-${REPO_TAG}"
BIN_DIR="$REPO/scripts/git-autosnapshot"

echo
hr "git-autosnapshot -> $REPO"
[ "$DRY_RUN" = 1 ] && printf '\033[1;36m== DRY RUN — nothing will be written ==\033[0m\n'

# ── uninstall path ───────────────────────────────────────────────────────────
if [ "$UNINSTALL" = 1 ]; then
  hr "Uninstall"
  if command -v systemctl >/dev/null 2>&1; then
    systemctl disable --now "${SVC}.timer" 2>/dev/null || true
    rm -f "/etc/systemd/system/${SVC}.timer" "/etc/systemd/system/${SVC}.service" 2>/dev/null || true
    systemctl daemon-reload 2>/dev/null || true
    ok "systemd timer removed (${SVC})"
  fi
  # cron fallback removal
  if crontab -l 2>/dev/null | grep -q "git-autosnapshot.*$REPO"; then
    crontab -l 2>/dev/null | grep -v "git-autosnapshot.*$REPO" | crontab - 2>/dev/null || true
    ok "cron entry removed"
  fi
  ok "Done. Snapshot store removed nothing; your files, commits, and any own repo are untouched."
  exit 0
fi

# ── pre-flight ───────────────────────────────────────────────────────────────
hr "Pre-flight"
command -v git >/dev/null 2>&1 || fail "git not found — install git first."
ok "git $(git --version | awk '{print $3}')"
# Init the ISOLATED snapshot git-dir (NOT $REPO/.git). This is the whole point:
# the user's own repo, if any, is left completely untouched.
if [ ! -f "$GIT_DIR/HEAD" ]; then
  if [ "$DRY_RUN" = 1 ]; then
    plan "git init --separate-git-dir style snapshot store at $GIT_DIR (isolated; your own .git untouched)"
  else
    # PURE standalone git-dir. NOT --separate-git-dir (that would drop a gitlink
    # .git FILE into the tree and CONVERT an existing user repo). We create the
    # store in isolation and only ever address it via --git-dir/--work-tree, so
    # the user's own $REPO/.git (if any) is never read, written, or replaced.
    mkdir -p "$GIT_DIR"
    git --git-dir="$GIT_DIR" --work-tree="$REPO" init -q
    # Make the store bare-of-worktree-config safe: point it at the work-tree and
    # keep it from ever trying to check out into the tree.
    git --git-dir="$GIT_DIR" config core.worktree "$REPO" 2>/dev/null || true
    git --git-dir="$GIT_DIR" config core.bare false 2>/dev/null || true
    ok "isolated snapshot store initialized at $GIT_DIR (your own repo untouched)"
  fi
else
  ok "snapshot store present: $GIT_DIR"
fi
BRANCH="$(g symbolic-ref --short -q HEAD 2>/dev/null || echo main)"
ok "branch: $BRANCH"

# ── scale-friendly git config (safe, idempotent) ─────────────────────────────
hr "Scale config"
if [ "$DRY_RUN" = 1 ]; then
  plan "git config core.fsmonitor / core.untrackedcache / feature.manyFiles = true"
else
  g config core.untrackedcache true 2>/dev/null || true
  g config core.fsmonitor true 2>/dev/null || true
  g config feature.manyFiles true 2>/dev/null || true
  ok "fsmonitor + untrackedcache + manyFiles enabled (keeps staging fast at scale)"
fi

# ── copy scripts into the repo ───────────────────────────────────────────────
hr "Scripts"
if [ "$DRY_RUN" = 1 ]; then
  plan "install auto-commit.sh + git-retention.sh -> $BIN_DIR/"
else
  mkdir -p "$BIN_DIR"
  for s in auto-commit.sh git-retention.sh dinomem-undo.sh; do
    if [ -f "$BIN_DIR/$s" ] && [ "$FORCE" = 0 ]; then
      skip "$s (exists, --force to overwrite)"
    else
      cp "$SELF_DIR/$s" "$BIN_DIR/$s" && chmod +x "$BIN_DIR/$s" && ok "$s"
    fi
  done
fi

# ── ignore rules go in the snapshot git-dir's OWN info/exclude ────────────────
# CRUCIAL for isolation: we do NOT drop a .gitignore into the user's working
# tree (that would pollute their repo and their diffs). The snapshot store's
# private info/exclude does the same job, invisible to the user and to any
# repo they run themselves.
hr "ignore rules (private to snapshot store)"
EXC="$GIT_DIR/info/exclude"
MARKER="# >>> dinomem git-autosnapshot ignores >>>"
if [ "$DRY_RUN" = 1 ]; then
  plan "append runtime-noise ignore block to $EXC (private; NOT the user's .gitignore)"
elif grep -qF "$MARKER" "$EXC" 2>/dev/null; then
  skip "info/exclude block (already present)"
else
  mkdir -p "$GIT_DIR/info" 2>/dev/null || true
  cat "$SELF_DIR/gitignore.snippet" >> "$EXC"
  ok "runtime-noise ignore block written to snapshot store (user's tree untouched)"
fi
# git-lfs: match dinomem's real installer pattern (detect -> attempt install ->
# warn+continue). The main installer already auto-installs Python via apt/brew/
# pyenv, so being squeamish about git-lfs would be inconsistent. Attempt via the
# available pkg manager; if it fails or none is present, degrade gracefully
# (snapshots still work, media just isn't lfs-tracked).
if [ "$DO_LFS" = 1 ] && ! command -v git-lfs >/dev/null 2>&1; then
  if [ "$DRY_RUN" = 1 ]; then
    plan "git-lfs not found -> attempt install via apt/brew/dnf/yum/apk/pacman"
  else
    warn "git-lfs not found — attempting install..."
    if command -v apt-get >/dev/null 2>&1; then
      apt-get update -q >/dev/null 2>&1 || true
      apt-get install -y git-lfs >/dev/null 2>&1 || true
    elif command -v brew >/dev/null 2>&1; then
      brew install git-lfs >/dev/null 2>&1 || true
    elif command -v dnf >/dev/null 2>&1; then
      dnf install -y git-lfs >/dev/null 2>&1 || true
    elif command -v yum >/dev/null 2>&1; then
      yum install -y git-lfs >/dev/null 2>&1 || true
    elif command -v apk >/dev/null 2>&1; then
      apk add git-lfs >/dev/null 2>&1 || true
    elif command -v pacman >/dev/null 2>&1; then
      pacman -S --noconfirm git-lfs >/dev/null 2>&1 || true
    fi
    if command -v git-lfs >/dev/null 2>&1; then
      ok "git-lfs installed"
    else
      warn "git-lfs install failed/unavailable — skipping media tracking (snapshots still work; install git-lfs manually for image/video handling)"
    fi
  fi
fi
if [ "$DO_LFS" = 1 ] && command -v git-lfs >/dev/null 2>&1; then
  if [ "$DRY_RUN" = 1 ]; then
    plan "git lfs install + copy .gitattributes media rules"
  else
    g lfs install --local >/dev/null 2>&1 || true
    # lfs media rules live in the snapshot store's info/attributes, NOT a
    # .gitattributes in the user's tree (same isolation rule as the ignores).
    ATTR="$GIT_DIR/info/attributes"
    if grep -qF 'dinomem git-autosnapshot' "$ATTR" 2>/dev/null; then
      skip "info/attributes media rules (already present)"
    else
      mkdir -p "$GIT_DIR/info" 2>/dev/null || true
      printf '# >>> dinomem git-autosnapshot media rules >>>\n' >> "$ATTR"
      cat "$SELF_DIR/gitattributes.template" >> "$ATTR"
      ok "media/lfs rules written to snapshot store (user's tree untouched)"
    fi
  fi
fi

# ── scheduler: systemd timer preferred, cron fallback ────────────────────────
hr "Scheduler (every ${INTERVAL_MIN} min)"
ENVLINE="AUTOSNAP_REPO=$REPO AUTOSNAP_GIT_DIR=$GIT_DIR AUTOSNAP_MAX_MB=$MAX_MB AUTOSNAP_RETAIN_DAYS=$RETAIN_DAYS AUTOSNAP_BRANCH=$BRANCH"
if command -v systemctl >/dev/null 2>&1 && [ -d /etc/systemd/system ] && [ -w /etc/systemd/system ]; then
  if [ "$DRY_RUN" = 1 ]; then
    plan "write /etc/systemd/system/${SVC}.{service,timer} + enable --now"
  else
    cat > "/etc/systemd/system/${SVC}.service" <<EOF
[Unit]
Description=dinomem git auto-snapshot for ${REPO}
After=network.target

[Service]
Type=oneshot
Environment=${ENVLINE}
ExecStart=${BIN_DIR}/auto-commit.sh
Nice=10
EOF
    cat > "/etc/systemd/system/${SVC}.timer" <<EOF
[Unit]
Description=Run dinomem git auto-snapshot every ${INTERVAL_MIN} min (${REPO})

[Timer]
OnBootSec=5min
OnUnitActiveSec=${INTERVAL_MIN}min
Persistent=true

[Install]
WantedBy=timers.target
EOF
    systemctl daemon-reload
    systemctl enable --now "${SVC}.timer" >/dev/null 2>&1
    ok "systemd timer active (${SVC}.timer, every ${INTERVAL_MIN}min)"
  fi
else
  # cron fallback
  CRON_LINE="*/${INTERVAL_MIN} * * * * ${ENVLINE} ${BIN_DIR}/auto-commit.sh >> ${REPO}/logs/git-autosnapshot.log 2>&1  # git-autosnapshot ${REPO}"
  if [ "$DRY_RUN" = 1 ]; then
    plan "register cron: $CRON_LINE"
  elif ! command -v crontab >/dev/null 2>&1; then
    warn "NO SCHEDULER: neither systemd nor crontab available. Scripts installed but nothing"
    warn "will run them automatically. Wire ${BIN_DIR}/auto-commit.sh into your own scheduler,"
    warn "e.g. run every ${INTERVAL_MIN}min with: ${ENVLINE} ${BIN_DIR}/auto-commit.sh"
  elif crontab -l 2>/dev/null | grep -q "git-autosnapshot.*$REPO"; then
    skip "cron entry (exists)"
  else
    { crontab -l 2>/dev/null; echo "$CRON_LINE"; } | crontab -
    ok "cron entry registered (systemd unavailable, using crontab)"
  fi
fi

# ── first snapshot ───────────────────────────────────────────────────────────
if [ "$DRY_RUN" != 1 ]; then
  hr "First snapshot"
  AUTOSNAP_REPO="$REPO" AUTOSNAP_GIT_DIR="$GIT_DIR" AUTOSNAP_MAX_MB="$MAX_MB" AUTOSNAP_RETAIN_DAYS="$RETAIN_DAYS" AUTOSNAP_BRANCH="$BRANCH" \
    "$BIN_DIR/auto-commit.sh" && ok "initial snapshot run complete" || warn "initial snapshot returned non-zero (check logs/git-autosnapshot.log)"

  # Safety net (defense-in-depth): the snapshot store object DB lives INSIDE the
  # work-tree. info/exclude is written BEFORE the first snapshot so its internals
  # never get tracked -- but a hand-init, a reordered future edit, or a
  # pre-existing store could slip them into the index. Once tracked, git ignore
  # rules can't retroactively untrack them, and every tick would then see git
  # internals as churn (a self-committing loop). This explicitly untracks any
  # store internals that reached the index, so the invariant holds regardless of
  # how the store was created. No-op on a clean install.
  if git --git-dir="$GIT_DIR" --work-tree="$REPO" ls-files -- '.dinomem-snap.git/' 2>/dev/null | grep -q .; then
    git --git-dir="$GIT_DIR" --work-tree="$REPO" rm -r --cached --quiet -- '.dinomem-snap.git' 2>/dev/null || true
    git --git-dir="$GIT_DIR" --work-tree="$REPO" commit --quiet \
      -m "chore: untrack snapshot store internals (self-ignore safety net)" 2>/dev/null || true
    ok "untracked snapshot-store internals from index (self-tracking guard)"
  fi
fi

echo
ok "git-autosnapshot installed. Snapshots every ${INTERVAL_MIN}min, disk-aware cleanup, ${RETAIN_DAYS}d retention."
echo "  Logs:      $REPO/logs/git-autosnapshot.log"
echo "  Uninstall: bash $SELF_DIR/install.sh --repo $REPO --uninstall"
