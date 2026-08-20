#!/usr/bin/env bash
# dinomem config-guard — installer.
# Installs an INDEPENDENT systemd watchdog that reverts openclaw.json to the last
# known-good snapshot whenever a write leaves it syntactically broken, so a stray
# trailing comma can never crash-loop the gateway.
#
# WHY a separate watchdog (not just `openclaw config patch`): patch = prevention
# but depends on the writer obeying. This guard = enforcement — it catches raw
# edits, manual vim/sed, plugin/cron writes, ANY path, and runs even while
# OpenClaw is crashing (it lives at the OS/systemd level, outside OpenClaw).
#
# Usage:
#   bash install.sh [--openclaw-dir DIR] [--system] [--force] [--dry-run] [--uninstall]
#
# Options:
#   --openclaw-dir DIR  OpenClaw home holding openclaw.json (default: $OPENCLAW_HOME or ~/.openclaw)
#   --system            install as a SYSTEM unit (/etc/systemd/system) instead of --user
#   --force             overwrite an existing guard script / units (default: skip if present)
#   --dry-run           preview only, write nothing
#   --uninstall         remove units + script + snapshot (keeps any .broken forensic copies)
#
# DUP-AWARE: if a guard script or units already exist, they are LEFT ALONE unless
# --force. Re-running without --force is a safe no-op (idempotent).
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OPENCLAW_DIR="${OPENCLAW_HOME:-$HOME/.openclaw}"
USE_SYSTEM=0
FORCE=0
DRY_RUN=0
UNINSTALL=0

while [ $# -gt 0 ]; do
  case "$1" in
    --openclaw-dir) OPENCLAW_DIR="$2"; shift 2 ;;
    --system)       USE_SYSTEM=1; shift ;;
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

OPENCLAW_DIR="$(cd "$OPENCLAW_DIR" 2>/dev/null && pwd || echo "$OPENCLAW_DIR")"
CFG="$OPENCLAW_DIR/openclaw.json"
SCRIPT_DST="$OPENCLAW_DIR/scripts/config-guard.sh"

# Unit destination: --user (default) vs --system. User is preferred (no root),
# matches the source-of-truth design; --system for boxes running the gateway as
# a system service where a user manager may not be running.
if [ "$USE_SYSTEM" = 1 ]; then
  UNIT_DIR="/etc/systemd/system"
  SYSTEMCTL="systemctl"
else
  UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  SYSTEMCTL="systemctl --user"
fi
SVC="openclaw-config-guard.service"
PATHUNIT="openclaw-config-guard.path"

echo
hr "config-guard -> $OPENCLAW_DIR"
[ "$DRY_RUN" = 1 ] && printf '\033[1;36m== DRY RUN — nothing will be written ==\033[0m\n'

# ── uninstall path ───────────────────────────────────────────────────────────
if [ "$UNINSTALL" = 1 ]; then
  hr "Uninstall"
  $SYSTEMCTL disable --now "$PATHUNIT" 2>/dev/null || true
  rm -f "$UNIT_DIR/$PATHUNIT" "$UNIT_DIR/$SVC" 2>/dev/null || true
  $SYSTEMCTL daemon-reload 2>/dev/null || true
  rm -f "$SCRIPT_DST" "${CFG}.guard-good" 2>/dev/null || true
  ok "config-guard removed (units + script + good-snapshot). Any .broken-* forensic copies kept."
  exit 0
fi

# ── pre-flight: hard deps ────────────────────────────────────────────────────
hr "Pre-flight"
MISSING=""
command -v jq    >/dev/null 2>&1 || MISSING="$MISSING jq"
command -v flock >/dev/null 2>&1 || MISSING="$MISSING flock"
if [ -n "$MISSING" ]; then
  # jq is THE core dep (the syntax gate). Attempt install of whatever's missing.
  warn "missing:$MISSING — attempting install (latest stable from your pkg manager)..."
  if [ "$DRY_RUN" = 0 ]; then
    if command -v apt-get >/dev/null 2>&1; then apt-get update -q >/dev/null 2>&1 || true; apt-get install -y $MISSING >/dev/null 2>&1 || true
    elif command -v brew  >/dev/null 2>&1; then brew install $MISSING >/dev/null 2>&1 || true
    elif command -v dnf   >/dev/null 2>&1; then dnf install -y $MISSING >/dev/null 2>&1 || true
    elif command -v yum   >/dev/null 2>&1; then yum install -y $MISSING >/dev/null 2>&1 || true
    elif command -v apk   >/dev/null 2>&1; then apk add $MISSING >/dev/null 2>&1 || true
    elif command -v pacman>/dev/null 2>&1; then pacman -S --noconfirm $MISSING >/dev/null 2>&1 || true
    fi
  fi
  command -v jq >/dev/null 2>&1 || fail "jq is required and could not be installed — install jq, then re-run."
  command -v flock >/dev/null 2>&1 || warn "flock still missing — guard runs without single-instance locking (acceptable, but install util-linux for full safety)."
fi
# systemd presence — graceful skip (script still installed, just not auto-armed).
NO_SYSTEMD=0
if ! command -v systemctl >/dev/null 2>&1; then
  NO_SYSTEMD=1
  warn "systemctl not found — will install the guard script but cannot arm the watcher. Wire $SCRIPT_DST into your own file-watch."
elif [ "$USE_SYSTEM" = 0 ] && ! $SYSTEMCTL show-environment >/dev/null 2>&1; then
  NO_SYSTEMD=1
  warn "systemd --user manager not available in this session — install the script, but arm manually (or re-run with --system)."
fi
ok "deps checked (jq present)"

# ── install the guard script (DUP-AWARE) ─────────────────────────────────────
hr "Guard script"
if [ "$DRY_RUN" = 1 ]; then
  plan "install config-guard.sh -> $SCRIPT_DST"
elif [ -f "$SCRIPT_DST" ] && [ "$FORCE" = 0 ]; then
  skip "config-guard.sh (exists at $SCRIPT_DST, --force to overwrite)"
else
  mkdir -p "$(dirname "$SCRIPT_DST")"
  cp "$SELF_DIR/config-guard.sh" "$SCRIPT_DST" && chmod +x "$SCRIPT_DST" && ok "config-guard.sh -> $SCRIPT_DST"
fi

# ── install units (DUP-AWARE), rewriting %h to the resolved absolute paths ────
# The templates use %h for the default ~/.openclaw layout; we always emit absolute
# paths so a non-default --openclaw-dir (or --system, where %h is unreliable) works.
hr "systemd units ($([ "$USE_SYSTEM" = 1 ] && echo system || echo user))"
emit_unit() {
  # $1 = template basename, $2 = destination
  local tmpl="$SELF_DIR/units/$1" dst="$2"
  if [ -f "$dst" ] && [ "$FORCE" = 0 ]; then
    skip "$(basename "$dst") (exists, --force to overwrite)"
    return 0
  fi
  # Rewrite %h/.openclaw/... placeholders to the real absolute paths.
  sed -e "s#%h/.openclaw/scripts/config-guard.sh#$SCRIPT_DST#g" \
      -e "s#%h/.openclaw/openclaw.json#$CFG#g" \
      "$tmpl" > "$dst"
  ok "$(basename "$dst")"
}
if [ "$DRY_RUN" = 1 ]; then
  plan "write $UNIT_DIR/{$SVC,$PATHUNIT} (abs paths: ExecStart=$SCRIPT_DST, watch=$CFG)"
else
  mkdir -p "$UNIT_DIR"
  emit_unit "$SVC" "$UNIT_DIR/$SVC"
  emit_unit "$PATHUNIT" "$UNIT_DIR/$PATHUNIT"
fi

# ── seed the good snapshot ONLY if the current config is valid ────────────────
hr "Seed good snapshot"
if [ "$DRY_RUN" = 1 ]; then
  plan "seed ${CFG}.guard-good from current config (only if jq empty passes)"
elif [ ! -f "$CFG" ]; then
  warn "no config at $CFG yet — snapshot will seed itself on the first valid write"
elif jq empty "$CFG" 2>/dev/null; then
  cp -f "$CFG" "${CFG}.guard-good" && ok "seeded ${CFG}.guard-good"
else
  warn "current $CFG is NOT valid JSON — refusing to seed a broken snapshot. Fix it first, then re-run (or the guard seeds on the next valid write)."
fi

# ── MANDATORY self-test on a FAKE file BEFORE arming (never touches real cfg) ──
hr "Self-test (fake file — real config untouched)"
if [ "$DRY_RUN" = 1 ]; then
  plan "run guard against a corrupted fake file; assert it auto-restores to valid"
elif [ ! -x "$SCRIPT_DST" ]; then
  warn "guard script not installed (skipped) — cannot self-test"
else
  T="$(mktemp -d)"
  echo '{"ok":true}' > "$T/fake.json"
  GUARD_CFG="$T/fake.json" GUARD_GOOD="$T/fake.json.guard-good" GUARD_LOG="$T/log" GUARD_LOCK="$T/lock" GUARD_SETTLE=0 "$SCRIPT_DST" || true
  printf '%s' '{"ok":true,,}' > "$T/fake.json"   # corrupt (double comma)
  GUARD_CFG="$T/fake.json" GUARD_GOOD="$T/fake.json.guard-good" GUARD_LOG="$T/log" GUARD_LOCK="$T/lock" GUARD_SETTLE=0 "$SCRIPT_DST" || true
  if jq empty "$T/fake.json" 2>/dev/null; then
    ok "self-test PASSED — corrupted fake file auto-restored to valid JSON"
    TEST_OK=1
  else
    TEST_OK=0
  fi
  # cleanup the throwaway test dir (best-effort; /tmp is auto-reaped regardless)
  rm -rf "$T" 2>/dev/null || true
  [ "${TEST_OK:-0}" = 1 ] || fail "self-test FAILED — NOT arming the watcher. Guard would not have restored a broken config. Inspect config-guard.sh."
fi

# ── arm the watcher (only after a passing self-test) ─────────────────────────
hr "Arm watcher"
if [ "$DRY_RUN" = 1 ]; then
  plan "$SYSTEMCTL daemon-reload && $SYSTEMCTL enable --now $PATHUNIT"
elif [ "$NO_SYSTEMD" = 1 ]; then
  warn "systemd unavailable — guard script installed at $SCRIPT_DST but NOT auto-armed. Run it from your own file-watch on $CFG."
else
  $SYSTEMCTL daemon-reload 2>/dev/null || true
  if $SYSTEMCTL enable --now "$PATHUNIT" >/dev/null 2>&1; then
    STATE="$($SYSTEMCTL is-active "$PATHUNIT" 2>/dev/null || echo unknown)"
    ok "watcher armed ($PATHUNIT: $STATE) — openclaw.json now auto-protected"
  else
    warn "could not enable $PATHUNIT — check '$SYSTEMCTL status $PATHUNIT'. Script is installed; arm manually."
  fi
fi

echo
ok "config-guard install complete."
echo "  Log:      $OPENCLAW_DIR/logs/config-guard.log"
echo "  Snapshot: ${CFG}.guard-good  (auto-refreshes on every valid write)"
echo "  Uninstall: bash $SELF_DIR/install.sh --uninstall$([ "$USE_SYSTEM" = 1 ] && echo ' --system')"
