#!/usr/bin/env bash
# dinomem — update script
# Re-runs install with --force to update scripts to latest version.
# Memory data and logs are preserved.
#
# Usage: bash scripts/update.sh [--workspace DIR] [--agent-id ID]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "$SCRIPT_DIR/install.sh" --force "$@"
