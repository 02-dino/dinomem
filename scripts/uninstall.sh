#!/usr/bin/env bash
# dinomem — uninstall script
# Removes: cron jobs, AGENTS.md block, openclaw.json patches, TEI Docker container.
# Does NOT delete memory data (memory/, logs/) unless --purge-data is passed.
#
# Usage:
#   bash scripts/uninstall.sh --workspace DIR --agent-id ID
#   bash scripts/uninstall.sh --workspace DIR --agent-id ID --purge         # also remove scripts
#   bash scripts/uninstall.sh --workspace DIR --agent-id ID --purge-data    # also remove memory data
set -euo pipefail

WS="${OPENCLAW_WORKSPACE:-$HOME/.openclaw/workspace}"
AGENT_ID=""
PURGE=0
PURGE_DATA=0

while [ $# -gt 0 ]; do
  case "$1" in
    --workspace)   WS="$2"; shift 2 ;;
    --agent-id)    AGENT_ID="$2"; shift 2 ;;
    --purge)       PURGE=1; shift ;;
    --purge-data)  PURGE_DATA=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

ok()   { printf '  \033[32m[ok]\033[0m   %s\n' "$*"; }
skip() { printf '  \033[33m[skip]\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m[warn]\033[0m %s\n' "$*"; }
hr()   { printf '\033[1m== %s ==\033[0m\n' "$*"; }

[ -z "$AGENT_ID" ] && AGENT_ID="$(basename "$WS")" && AGENT_ID="${AGENT_ID#workspace-}"

echo; hr "dinomem uninstall -> $WS (agent: $AGENT_ID)"

# ── Cron jobs ─────────────────────────────────────────────────────────────────
hr "Cron jobs"
CRON_PATTERNS=(
  "auto_session_reset.py"
  "memory_cleanup.py"
  "memory_review.py"
  "docker-compose.tei.yml"
  "dinomem:"
)
CURRENT_CRON="$(crontab -l 2>/dev/null || true)"
NEW_CRON="$CURRENT_CRON"
for pattern in "${CRON_PATTERNS[@]}"; do
  if echo "$NEW_CRON" | grep -qF "$pattern"; then
    NEW_CRON="$(echo "$NEW_CRON" | grep -vF "$pattern")"
    ok "removed cron: $pattern"
  else
    skip "cron not found: $pattern"
  fi
done
echo "$NEW_CRON" | crontab - 2>/dev/null || true

# ── AGENTS.md block ───────────────────────────────────────────────────────────
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

# ── openclaw.json patches ─────────────────────────────────────────────────────
hr "openclaw.json"
OPENCLAW_DIR="$(dirname "$WS")"
CONFIG="$OPENCLAW_DIR/openclaw.json"
if [ -f "$CONFIG" ]; then
  python3 - "$CONFIG" <<'PYEOF'
import json, sys
path = sys.argv[1]
with open(path) as f:
    cfg = json.load(f)

changed = []

# Revert session reset
sr = cfg.get("session", {}).get("reset", {})
if sr.get("mode") == "idle":
    sr.pop("mode", None)
    sr.pop("idleMinutes", None)
    changed.append("session.reset")

# Revert contextPruning
cp = cfg.get("contextPruning", {})
if cp.get("mode") == "off":
    cp.pop("mode", None)
    changed.append("contextPruning.mode")

# Revert compaction
comp = cfg.get("compaction", {})
if comp.get("mode") == "safeguard":
    comp.pop("mode", None)
    changed.append("compaction.mode")
mf = comp.get("memoryFlush", {})
if mf.get("enabled") is False:
    mf.pop("enabled", None)
    changed.append("compaction.memoryFlush.enabled")

# Revert workspaceBootstrap (only if explicitly set)
if "workspaceBootstrap" in defaults:
    defaults.pop("workspaceBootstrap")
    changed.append("workspaceBootstrap")

# Revert memorySearch
ms = cfg.get("memorySearch", {})
if ms.get("provider") == "openai-compatible":
    ms.pop("provider", None)
    ms.pop("remote", None)
    changed.append("memorySearch")

with open(path, "w") as f:
    json.dump(cfg, f, indent=2)

if changed:
    print("  reverted: " + ", ".join(changed))
else:
    print("  nothing to revert")
PYEOF
  ok "openclaw.json reverted"
else
  skip "openclaw.json not found at $CONFIG"
fi

# ── TEI Docker ────────────────────────────────────────────────────────────────
hr "TEI Docker"
if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -q "^tei-embed$"; then
  docker stop tei-embed >/dev/null 2>&1 && ok "stopped tei-embed"
  docker rm tei-embed >/dev/null 2>&1 && ok "removed tei-embed container"
else
  skip "tei-embed container not found"
fi

# ── Purge scripts (optional) ──────────────────────────────────────────────────
if [ "$PURGE" = 1 ]; then
  hr "Purge scripts"
  for f in \
    procedures/session_reset.py \
    procedures/auto_session_reset.py \
    procedures/extract_memory.py \
    tools/memory_cleanup.py \
    tools/memory_review.py \
    tools/config_tool.py; do
    [ -f "$WS/$f" ] && rm "$WS/$f" && ok "removed $f" || skip "$f not found"
  done
fi

# ── Purge data (optional, explicit) ───────────────────────────────────────────
if [ "$PURGE_DATA" = 1 ]; then
  hr "Purge data"
  warn "This will permanently delete memory data."
  read -r -p "  Type 'yes' to confirm: " confirm
  if [ "$confirm" = "yes" ]; then
    [ -d "$WS/memory" ] && rm -rf "$WS/memory" && ok "removed memory/"
    [ -d "$WS/logs" ]   && rm -rf "$WS/logs"   && ok "removed logs/"
    [ -f "$WS/MEMORY.md" ] && rm "$WS/MEMORY.md" && ok "removed MEMORY.md"
  else
    skip "purge-data cancelled"
  fi
fi

echo; hr "done"
echo "  Run 'openclaw gateway restart' to apply config changes."
