#!/usr/bin/env bash
# dinomem rea — installer shim for the REA reverse-engineering capability.
#
# WHY dinomem ships this: dinomem gives an agent MEMORY; REA gives it the
# ability to MEASURE an unknown artifact (a site, an APK, a binary, a minified
# bundle) instead of guessing from its name. Those compose — "what did we learn
# about X" is only as good as the agent's ability to look at X in the first
# place. Wiring it here means one install, not two.
#
# This is a SHIM, not a fork: it fetches 02-dino/rea-installer and delegates,
# always with --harness openclaw (dinomem is an OpenClaw plugin; the installer
# also supports claude/cursor/codex/generic, which are not our target). Keeping
# it a shim means REA upgrades land upstream, not in a stale copy here.
#
# NETWORK: needs git + GitHub reachability. Every failure path is non-fatal —
# dinomem's own install must never be blocked by an optional capability.
#
# Usage:
#   bash install.sh [--workspace DIR] [--no-ghidra] [--force] [--dry-run] [--uninstall]
#
# Options:
#   --workspace DIR  workspace whose AGENTS.md gets the REA trigger block
#   --no-ghidra      skip the Ghidra decompiler (web/JS/APK still work)
#   --force          refresh SKILL.md + trigger block even if present
#   --dry-run        preview only, write nothing
#   --uninstall      not supported here; prints the upstream removal hint
#
# Idempotent: re-running without --force is a safe no-op (upstream skips an
# already-present skill/trigger).
set -uo pipefail

REA_REPO="${REA_REPO:-https://github.com/02-dino/rea-installer}"
WS=""
EXTRA=()
DRY_RUN=0

while [ $# -gt 0 ]; do
  case "$1" in
    --workspace)  WS="$2"; shift 2 ;;
    --no-ghidra)  EXTRA+=(--no-ghidra); shift ;;
    --force)      EXTRA+=(--force); shift ;;
    --dry-run)    DRY_RUN=1; shift ;;
    --uninstall)
      echo "  rea is installed by an upstream script; remove it with that repo's"
      echo "  instructions (skill dir + the REA block in AGENTS.md)."
      exit 0 ;;
    -h|--help)    grep -E '^#( |$)' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

ok()   { printf '  \033[32m[ok]\033[0m   %s\n' "$*"; }
warn() { printf '  \033[33m[warn]\033[0m %s\n' "$*"; }
plan() { printf '  \033[36m[plan]\033[0m %s\n' "$*"; }

if ! command -v git >/dev/null 2>&1; then
  warn "git not found — skipping REA (install git and re-run to add it)"
  exit 0
fi

if [ "$DRY_RUN" = 1 ]; then
  plan "clone $REA_REPO and run install.sh --harness openclaw${WS:+ --workspace $WS}"
  exit 0
fi

# Clone into a private temp dir we own, so a half-finished clone can never be
# mistaken for a good one and nothing is left behind in the user's tree.
TMP="$(mktemp -d "${TMPDIR:-/tmp}/dinomem-rea.XXXXXX")" || {
  warn "could not create temp dir — skipping REA"; exit 0; }
cleanup() { [ -n "${TMP:-}" ] && [ -d "$TMP" ] && rm -rf -- "$TMP"; }
trap cleanup EXIT

if ! timeout 180 git clone -q --depth 1 "$REA_REPO" "$TMP/rea-installer" 2>/dev/null; then
  warn "could not fetch rea-installer (offline or unreachable) — skipping REA"
  exit 0
fi

REA_INSTALLER="$TMP/rea-installer/install.sh"
[ -f "$REA_INSTALLER" ] || { warn "rea-installer/install.sh missing — skipping REA"; exit 0; }

ARGS=(--harness openclaw)
[ -n "$WS" ] && ARGS+=(--workspace "$WS")
ARGS+=("${EXTRA[@]+"${EXTRA[@]}"}")

if bash "$REA_INSTALLER" "${ARGS[@]}"; then
  ok "REA wired for OpenClaw (run 'rea --help' to verify)"
else
  warn "rea-installer returned non-zero — dinomem install continues regardless"
fi
exit 0
