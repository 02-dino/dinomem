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
#   --all-homes         install/uninstall for EVERY OpenClaw home found next to the
#                       default one (~/.openclaw and ~/.openclaw-*). A box can run
#                       many gateways, each with its OWN openclaw.json; guarding one
#                       leaves the rest unprotected while still reporting "armed".
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
ALL_HOMES=0

while [ $# -gt 0 ]; do
  case "$1" in
    --openclaw-dir) OPENCLAW_DIR="$2"; shift 2 ;;
    --all-homes)    ALL_HOMES=1; shift ;;
    --system)       USE_SYSTEM=1; shift ;;
    --force)        FORCE=1; shift ;;
    --dry-run)      DRY_RUN=1; shift ;;
    --uninstall)    UNINSTALL=1; shift ;;
    -h|--help)      grep -E '^#( |$)' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

# ── multi-home fan-out ───────────────────────────────────────────────────────
# A box can run MANY gateways, each with its own config home (measured
# 2026-11-02: 14 live gateways, 14 homes, only 1 guarded). Guarding one home
# while the operator believes the box is covered is the failure this flag
# closes. Re-exec once per discovered home; each child does the normal
# single-home install, so there is exactly ONE install code path.
if [ "$ALL_HOMES" = 1 ]; then
  _base_parent="$(dirname "$OPENCLAW_DIR")"
  _base_name="$(basename "$OPENCLAW_DIR")"
  _homes=()
  for _h in "$OPENCLAW_DIR" "$_base_parent/$_base_name"-*; do
    [ -d "$_h" ] || continue
    [ -f "$_h/openclaw.json" ] || continue
    _homes+=("$_h")
  done
  if [ "${#_homes[@]}" -eq 0 ]; then
    echo "no OpenClaw home with an openclaw.json found under $_base_parent" >&2
    exit 1
  fi
  printf '\033[1m== config-guard: %d home(s) ==\033[0m\n' "${#_homes[@]}"
  _rc=0
  for _h in "${_homes[@]}"; do
    _args=(--openclaw-dir "$_h")
    [ "$USE_SYSTEM" = 1 ] && _args+=(--system)
    [ "$FORCE" = 1 ]      && _args+=(--force)
    [ "$DRY_RUN" = 1 ]    && _args+=(--dry-run)
    [ "$UNINSTALL" = 1 ]  && _args+=(--uninstall)
    # Never abort the fan-out on one bad home: a single unwritable or
    # half-provisioned home must not leave the remaining gateways unguarded.
    bash "${BASH_SOURCE[0]}" "${_args[@]}" || { _rc=1; printf '  \033[33m[warn]\033[0m %s failed — continuing with the remaining homes\n' "$_h"; }
  done
  exit "$_rc"
fi

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
# INSTANCE NAME = this home's path, escaped. A box can run many gateways, each
# with its OWN openclaw.json (measured 2026-11-02: 14 live gateways, 14 homes).
# With the old FIXED unit names a second install did not widen coverage, it
# OVERWROTE the first — so every extra home sat unguarded while systemd still
# reported one happy "active" unit. Template units let N homes coexist.
if command -v systemd-escape >/dev/null 2>&1; then
  INSTANCE="$(systemd-escape --path "$OPENCLAW_DIR")"
else
  # No systemd-escape (no systemd at all): any stable string will do, because
  # without systemd we never emit units anyway.
  INSTANCE="$(printf '%s' "${OPENCLAW_DIR#/}" | tr '/' '-')"
fi
SVC="openclaw-config-guard@${INSTANCE}.service"
PATHUNIT="openclaw-config-guard@${INSTANCE}.path"
SVC_TMPL="openclaw-config-guard@.service"
PATH_TMPL="openclaw-config-guard@.path"
# Pre-template installs left these behind; they watch ONE hardcoded path and
# would keep firing alongside the new instances.
LEGACY_SVC="openclaw-config-guard.service"
LEGACY_PATH="openclaw-config-guard.path"

echo
hr "config-guard -> $OPENCLAW_DIR"
[ "$DRY_RUN" = 1 ] && printf '\033[1;36m== DRY RUN — nothing will be written ==\033[0m\n'

# ── uninstall path ───────────────────────────────────────────────────────────
if [ "$UNINSTALL" = 1 ]; then
  hr "Uninstall"
  $SYSTEMCTL disable --now "$PATHUNIT" 2>/dev/null || true
  rm -f "$UNIT_DIR/$PATHUNIT" "$UNIT_DIR/$SVC" 2>/dev/null || true
  # Legacy (pre-template) units watched ONE hardcoded path. Leaving them armed
  # after an upgrade means a stale guard keeps firing on a home nobody asked for.
  $SYSTEMCTL disable --now "$LEGACY_PATH" 2>/dev/null || true
  rm -f "$UNIT_DIR/$LEGACY_PATH" "$UNIT_DIR/$LEGACY_SVC" 2>/dev/null || true
  # Templates are shared by every instance — only remove them once no instance
  # is left, otherwise uninstalling one home disarms all the others.
  if ! ls "$UNIT_DIR"/openclaw-config-guard@*.path >/dev/null 2>&1; then
    rm -f "$UNIT_DIR/$SVC_TMPL" "$UNIT_DIR/$PATH_TMPL" 2>/dev/null || true
  fi
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

# ── install the TEMPLATE units ───────────────────────────────────────────────
# Copied verbatim: %I is expanded by systemd at runtime to this instance's home,
# so ONE pair of template files serves every home on the box. Nothing is
# rewritten per home anymore — that rewriting is exactly what forced a single
# fixed unit name and capped the feature at one gateway.
hr "systemd units ($([ "$USE_SYSTEM" = 1 ] && echo system || echo user))"
emit_tmpl() {
  # $1 = template basename (shared by all instances)
  local src="$SELF_DIR/units/$1" dst="$UNIT_DIR/$1"
  [ -f "$src" ] || { warn "missing template $1 — cannot arm"; return 1; }
  # Templates are SHARED, so refresh whenever content differs: skipping here
  # would pin every home to the first-installed version.
  if [ -f "$dst" ] && cmp -s "$src" "$dst"; then
    skip "$1 (up-to-date)"
    return 0
  fi
  cp "$src" "$dst" && ok "$1"
}
if [ "$DRY_RUN" = 1 ]; then
  plan "install templates $PATH_TMPL + $SVC_TMPL into $UNIT_DIR"
  plan "enable instance $PATHUNIT (watch=$CFG, OPENCLAW_DIR=$OPENCLAW_DIR)"
else
  mkdir -p "$UNIT_DIR"
  emit_tmpl "$SVC_TMPL"
  emit_tmpl "$PATH_TMPL"
  # Retire a pre-template install: it watches ONE hardcoded path and would keep
  # firing next to the new instances, so an upgrade must disarm it explicitly.
  if [ -f "$UNIT_DIR/$LEGACY_PATH" ] || [ -f "$UNIT_DIR/$LEGACY_SVC" ]; then
    $SYSTEMCTL disable --now "$LEGACY_PATH" >/dev/null 2>&1 || true
    rm -f "$UNIT_DIR/$LEGACY_PATH" "$UNIT_DIR/$LEGACY_SVC" 2>/dev/null || true
    ok "retired legacy fixed-name units (replaced by per-home instances)"
  fi
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
  # Leg 2: run it the way SYSTEMD will — with a stripped environment. The shell
  # test above inherits HOME/PATH, so it passed on a box where the real unit
  # died instantly on `HOME: unbound variable` and the installer still reported
  # "armed" (measured 2026-11-02). A guard that only works in an interactive
  # shell is not a guard; test the env the service actually gets.
  if [ "$TEST_OK" = 1 ] && command -v env >/dev/null 2>&1; then
    echo '{"ok":true}' > "$T/bare.json"
    env -i GUARD_CFG="$T/bare.json" GUARD_GOOD="$T/bare.json.guard-good" \
           GUARD_LOG="$T/log2" GUARD_LOCK="$T/lock2" GUARD_SETTLE=0 \
           /bin/bash "$SCRIPT_DST" >/dev/null 2>"$T/bare.err" || true
    printf '%s' '{"ok":true,,}' > "$T/bare.json"
    env -i GUARD_CFG="$T/bare.json" GUARD_GOOD="$T/bare.json.guard-good" \
           GUARD_LOG="$T/log2" GUARD_LOCK="$T/lock2" GUARD_SETTLE=0 \
           /bin/bash "$SCRIPT_DST" >/dev/null 2>>"$T/bare.err" || true
    if jq empty "$T/bare.json" 2>/dev/null; then
      ok "self-test PASSED under a stripped env (systemd-like: no HOME/PATH)"
    else
      TEST_OK=0
      warn "guard FAILED with a stripped environment — this is exactly what the systemd unit gets:"
      sed -n '1,5p' "$T/bare.err" 2>/dev/null | while IFS= read -r _l; do warn "  $_l"; done
    fi
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
