#!/usr/bin/env bash
# dinomem — update script
# Re-runs install with --force to update scripts to latest version.
# Memory data and logs are preserved.
#
# Usage: bash scripts/update.sh [--workspace DIR] [--agent-id ID]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

ok()   { printf '  \033[32m[ok]\033[0m   %s\n' "$*"; }
warn() { printf '  \033[33m[warn]\033[0m %s\n' "$*"; }
hr()   { printf '\033[1m== %s ==\033[0m\n' "$*"; }

# Capture version before pull
VERSION_FILE="$REPO_DIR/VERSION"
PRE_VERSION="(unknown)"
[ -f "$VERSION_FILE" ] && PRE_VERSION=$(cat "$VERSION_FILE" | tr -d '[:space:]')

# Pull latest from GitHub before updating
hr "Pulling latest dinomem"
if [ -d "$REPO_DIR/.git" ]; then
  PULL_OUT=$(git -C "$REPO_DIR" pull --ff-only 2>&1) && echo "$PULL_OUT" || { warn "git pull failed — running update with local version"; echo "$PULL_OUT"; }
else
  warn "Not a git repo — skipping pull, running update with local version"
fi

# Capture version after pull
POST_VERSION="(unknown)"
[ -f "$VERSION_FILE" ] && POST_VERSION=$(cat "$VERSION_FILE" | tr -d '[:space:]')

# Version change notification
hr "Version check"
if [ "$PRE_VERSION" = "(unknown)" ] || [ "$POST_VERSION" = "(unknown)" ]; then
  warn "Could not read VERSION file — skipping version check"
elif [ "$PRE_VERSION" = "$POST_VERSION" ]; then
  ok "Already on latest: v$POST_VERSION"
else
  echo ""
  printf '  \033[32m✨ dinomem updated: v%s → v%s\033[0m\n' "$PRE_VERSION" "$POST_VERSION"
  echo ""
  echo "  ⚠️  Run the update to apply changes to your workspace:"
  echo "      bash $SCRIPT_DIR/install.sh --force [--workspace DIR] [--agent-id ID]"
  echo ""
  echo "  Or continue below — this script will apply it automatically."
  echo ""
fi

exec bash "$SCRIPT_DIR/install.sh" --force "$@"
