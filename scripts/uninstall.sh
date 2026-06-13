#!/usr/bin/env bash
# dinomem — uninstall script
# Removes dinomem cron jobs and AGENTS.md block. Does NOT delete scripts or memory data.
#
# Usage: bash scripts/uninstall.sh [--workspace DIR] [--agent-id ID] [--purge]
set -euo pipefail

WS="${OPENCLAW_WORKSPACE:-$HOME/.openclaw/workspace}"
AGENT_ID=""
PURGE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --workspace) WS="$2"; shift 2 ;;
    --agent-id)  AGENT_ID="$2"; shift 2 ;;
    --purge)     PURGE=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

ok()   { printf '  \033[32m[ok]\033[0m   %s\n' "$*"; }
skip() { printf '  \033[33m[skip]\033[0m %s\n' "$*"; }
hr()   { printf '\033[1m== %s ==\033[0m\n' "$*"; }

[ -z "$AGENT_ID" ] && AGENT_ID="$(basename "$WS")" && AGENT_ID="${AGENT_ID#workspace-}"

echo; hr "dinomem uninstall -> $WS (agent: $AGENT_ID)"

# Remove cron jobs
hr "Cron jobs"
if crontab -l 2>/dev/null | grep -q "auto_session_reset.py"; then
  crontab -l 2>/dev/null | grep -v "auto_session_reset.py" | grep -v "dinomem" | crontab -
  ok "auto_session_reset cron removed"
else
  skip "no dinomem cron found"
fi

if crontab -l 2>/dev/null | grep -q "docker-compose.tei.yml"; then
  crontab -l 2>/dev/null | grep -v "docker-compose.tei.yml" | crontab -
  ok "TEI @reboot cron removed"
fi

# Remove AGENTS.md block
hr "AGENTS.md"
AGENTS="$WS/AGENTS.md"
BEGIN="<!-- BEGIN:dinomem (managed — do not edit between markers) -->"
END="<!-- END:dinomem -->"
if [ -f "$AGENTS" ] && grep -qF "$BEGIN" "$AGENTS"; then
  awk -v b="$BEGIN" -v e="$END" '$0==b{skip=1} $0==e{skip=0; next} !skip{print}' "$AGENTS" > "$AGENTS.tmp" && mv "$AGENTS.tmp" "$AGENTS"
  ok "AGENTS.md block removed"
else
  skip "AGENTS.md block not found"
fi

# Purge scripts (optional)
if [ "$PURGE" = 1 ]; then
  hr "Purge scripts"
  for f in procedures/session_reset.py procedures/auto_session_reset.py procedures/extract_memory.py tools/memory_cleanup.py tools/memory_review.py; do
    [ -f "$WS/$f" ] && rm "$WS/$f" && ok "removed $f" || skip "$f not found"
  done
  warn "Memory data (memory/, logs/) preserved. Delete manually if needed."
fi

echo; hr "done"
