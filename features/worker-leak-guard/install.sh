#!/usr/bin/env bash
# dinomem worker-leak-guard — installer.
# Registers a cron job that trims ORPHANED OpenClaw inference workers
# (`openclaw-infer` / `openclaw-message` reparented to init).
#
# WHY THIS EXISTS
#   dinomem drives the OpenClaw CLI under a timeout. Python's
#   subprocess.run(timeout=N) kills only the DIRECT child, so the CLI's own
#   inference worker (300-600MB) survives, reparents to init, and nobody ever
#   reads its output. Measured on an 11-agent box (2026-10-19): ~2.3 orphans a
#   minute, 22 alive at once (~8GB), load 81 on 8 cores — which then made a
#   gateway miss its shutdown deadline and get SIGKILLed mid config write.
#
#   dinomem fixes the SOURCE (process-group kill in extract_memory.py). This
#   guard is the SAFETY NET for the same bug class: any other leaking path, a
#   version skew, or a half-updated install. Source fix = prevention; guard =
#   enforcement, the same split as features/config-guard.
#
# SAFETY: only ppid=1 workers past an age floor are touched. A worker with a
#   living parent is never killed, at any age or size — see the script header.
#
# Usage:
#   bash install.sh [--min-age-min N] [--every-min N] [--force] [--dry-run] [--uninstall]
#
# Options:
#   --min-age-min N   orphan age floor, minutes (default 3)
#   --every-min N     how often cron runs the guard, minutes (default 5)
#   --force           overwrite an existing guard script (default: keep, just re-register cron)
#   --dry-run         preview only, write nothing
#   --uninstall       remove the cron line (leaves the script on disk)
#
# Idempotent: re-running updates the single tagged cron line in place; it never
# appends a second copy, and never touches untagged user jobs.
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OPENCLAW_DIR="${OPENCLAW_HOME:-$HOME/.openclaw}"
MIN_AGE_MIN=3
EVERY_MIN=5
FORCE=0
DRY_RUN=0
UNINSTALL=0

while [ $# -gt 0 ]; do
  case "$1" in
    --openclaw-dir) OPENCLAW_DIR="$2"; shift 2 ;;
    --min-age-min)  MIN_AGE_MIN="$2"; shift 2 ;;
    --every-min)    EVERY_MIN="$2"; shift 2 ;;
    --force)        FORCE=1; shift ;;
    --dry-run)      DRY_RUN=1; shift ;;
    --uninstall)    UNINSTALL=1; shift ;;
    -h|--help)      grep -E '^#( |$)' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

ok()   { printf '  \033[32m[ok]\033[0m   %s\n' "$*"; }
warn() { printf '  \033[33m[warn]\033[0m %s\n' "$*"; }
plan() { printf '  \033[36m[plan]\033[0m %s\n' "$*"; }

TAG="# dinomem-managed:worker-leak-guard"
DEST_DIR="$OPENCLAW_DIR/scripts"
DEST="$DEST_DIR/worker-leak-guard.sh"
LOG="$OPENCLAW_DIR/logs/worker-leak-guard.log"
SRC="$SELF_DIR/worker-leak-guard.sh"

if ! command -v crontab >/dev/null 2>&1; then
  warn "crontab not found — guard not registered (script still usable manually)"
  exit 0
fi

# Rewrite the crontab through a temp file and verify the line delta before
# installing. NEVER pipe a producer straight into `crontab -`: under pipefail a
# failing producer emits nothing and `crontab -` happily installs an EMPTY
# crontab (same trap documented in scripts/install.sh).
write_crontab() {   # $1 = new content file, $2 = expected delta (+1/0/-1)
  local new=$1 delta=$2 before after
  before=$(crontab -l 2>/dev/null | wc -l || echo 0)
  after=$(wc -l < "$new")
  if [ "$after" -ne $((before + delta)) ]; then
    warn "cron write refused: line count $before -> $after (expected delta $delta)"
    return 1
  fi
  crontab "$new"
}

if [ "$UNINSTALL" = 1 ]; then
  if crontab -l 2>/dev/null | grep -qF "$TAG"; then
    tmp=$(mktemp)
    crontab -l 2>/dev/null | grep -vF "$TAG" > "$tmp" || true
    if [ "$DRY_RUN" = 1 ]; then
      plan "remove cron line tagged '$TAG'"
    else
      write_crontab "$tmp" -1 && ok "cron line removed" || warn "cron removal skipped"
    fi
    rm -f "$tmp"
  else
    ok "no cron line to remove"
  fi
  echo "  (script left at $DEST — delete manually if you want it gone)"
  exit 0
fi

# ── 1) place the guard script ───────────────────────────────────────────────
if [ ! -f "$SRC" ]; then
  warn "worker-leak-guard.sh missing next to this installer — aborting"
  exit 1
fi

if [ "$DRY_RUN" = 1 ]; then
  plan "install $SRC -> $DEST"
elif [ -f "$DEST" ] && [ "$FORCE" != 1 ]; then
  ok "guard script already present (use --force to overwrite): $DEST"
else
  mkdir -p "$DEST_DIR" "$(dirname "$LOG")"
  install -m 0755 "$SRC" "$DEST"
  ok "guard script installed: $DEST"
fi

# ── 2) register cron (replace-in-place on the tag) ──────────────────────────
LINE="*/$EVERY_MIN * * * * /bin/bash $DEST --min-age-min $MIN_AGE_MIN >> $LOG 2>&1 $TAG"

if crontab -l 2>/dev/null | grep -qF "$TAG"; then
  if [ "$DRY_RUN" = 1 ]; then
    plan "update existing cron line (every ${EVERY_MIN}m, min age ${MIN_AGE_MIN}m)"
  else
    tmp=$(mktemp)
    # Replace the tagged line, keep every other line byte-identical. Delta 0.
    crontab -l 2>/dev/null | awk -v tag="$TAG" -v new="$LINE" \
      'index($0, tag) > 0 { print new; next } { print }' > "$tmp"
    write_crontab "$tmp" 0 && ok "cron line updated (every ${EVERY_MIN}m)" \
      || warn "cron update skipped"
    rm -f "$tmp"
  fi
else
  if [ "$DRY_RUN" = 1 ]; then
    plan "register cron: $LINE"
  else
    tmp=$(mktemp)
    crontab -l 2>/dev/null > "$tmp" || true
    echo "$LINE" >> "$tmp"
    write_crontab "$tmp" 1 && ok "cron registered (every ${EVERY_MIN}m, orphans older than ${MIN_AGE_MIN}m)" \
      || warn "cron registration skipped"
    rm -f "$tmp"
  fi
fi

echo
echo "  worker-leak-guard active. Check:  tail -f $LOG"
echo "  Preview without killing:          bash $DEST --dry-run"
echo "  Remove:                           bash $SELF_DIR/install.sh --uninstall"
