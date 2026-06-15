#!/usr/bin/env bash
# dinomem — uninstall script
# Removes: cron jobs, AGENTS.md block, openclaw.json patches, TEI Docker container.
# Does NOT delete memory data unless --purge-data or --purge-memory is passed.
#
# Usage:
#   bash scripts/uninstall.sh --workspace DIR --agent-id ID
#   bash scripts/uninstall.sh --workspace DIR --agent-id ID --purge          # also remove scripts
#   bash scripts/uninstall.sh --workspace DIR --agent-id ID --purge-data     # remove logs, snapshots (NOT memory)
#   bash scripts/uninstall.sh --workspace DIR --agent-id ID --purge-memory   # ⚠️  WIPES memory/, MEMORY.md — irreversible
set -euo pipefail

WS="${OPENCLAW_WORKSPACE:-$HOME/.openclaw/workspace}"
AGENT_ID=""
PURGE=0
PURGE_DATA=0
PURGE_MEMORY=0

while [ $# -gt 0 ]; do
  case "$1" in
    --workspace)     WS="$2"; shift 2 ;;
    --agent-id)      AGENT_ID="$2"; shift 2 ;;
    --purge)         PURGE=1; shift ;;
    --purge-data)    PURGE_DATA=1; shift ;;
    --purge-memory)  PURGE_MEMORY=1; shift ;;
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
  "workspace_backup.py"
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
defaults = cfg.get("agents", {}).get("defaults", {})

# Revert session reset
sr = cfg.get("session", {}).get("reset", {})
if sr.get("mode") == "idle":
    sr.pop("mode", None)
    sr.pop("idleMinutes", None)
    changed.append("session.reset")

# Revert contextPruning
cp = defaults.get("contextPruning", {})
if cp.get("mode") == "off":
    cp.pop("mode", None)
    changed.append("contextPruning.mode")

# Revert compaction
comp = defaults.get("compaction", {})
if comp.get("mode") == "safeguard":
    comp.pop("mode", None)
    changed.append("compaction.mode")
if "truncateAfterCompaction" in comp:
    comp.pop("truncateAfterCompaction", None)
    changed.append("compaction.truncateAfterCompaction")
mf = comp.get("memoryFlush", {})
if mf.get("enabled") is False:
    mf.pop("enabled", None)
    changed.append("compaction.memoryFlush.enabled")
if "softThresholdTokens" in mf:
    mf.pop("softThresholdTokens", None)
    changed.append("compaction.memoryFlush.softThresholdTokens")
# Remove tei-embed provider
providers = cfg.get("models", {}).get("providers", {})
if "tei-embed" in providers:
    del providers["tei-embed"]
    changed.append("models.providers.tei-embed")

# Revert workspaceBootstrap
if "workspaceBootstrap" in defaults:
    defaults.pop("workspaceBootstrap")
    changed.append("workspaceBootstrap")

# Revert memorySearch
ms = defaults.get("memorySearch", {})
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
    procedures/memory_cleanup.py \
    procedures/memory_review.py \
    procedures/workspace_backup.py \
    tools/config_tool.py; do
    [ -f "$WS/$f" ] && rm "$WS/$f" && ok "removed $f" || skip "$f not found"
  done
fi

# ── OpenClaw cron (Daily Note Review) ────────────────────────────────────────
hr "OpenClaw cron"
python3 - <<PYEOF
import subprocess, json
try:
    r = subprocess.run(['openclaw', 'cron', 'list', '--json'], capture_output=True, text=True, timeout=10)
    jobs = json.loads(r.stdout) if r.returncode == 0 else []
    job_list = jobs if isinstance(jobs, list) else jobs.get('jobs', {}).get('jobs', [])
    targets = [j for j in job_list if 'note' in j.get('name','').lower() and 'review' in j.get('name','').lower()]
    if not targets:
        print("  \033[33m[skip]\033[0m Daily Note Review cron not found")
    else:
        for j in targets:
            jid = j.get('id') or j.get('jobId')
            subprocess.run(['openclaw', 'cron', 'remove', jid], capture_output=True, timeout=10)
            print(f"  \033[32m[ok]\033[0m   removed OpenClaw cron: {j.get('name')} ({jid})")
except Exception as e:
    print(f"  \033[33m[warn]\033[0m Could not remove OpenClaw cron: {e}")
PYEOF

# ── Purge data: logs + snapshots (safe, no memory) ───────────────────────────
if [ "$PURGE_DATA" = 1 ]; then
  hr "Purge data (logs + snapshots)"
  [ -d "$WS/logs" ] && rm -rf "$WS/logs" && ok "removed logs/" || skip "logs/ not found"
  if [ -d "$WS/.backups/snapshots" ]; then
    rm -rf "$WS/.backups/snapshots" && ok "removed .backups/snapshots/"
  else
    skip ".backups/snapshots not found"
  fi
  warn "memory/ and MEMORY.md preserved — use --purge-memory to wipe them"
fi

# ── Purge memory (explicit, irreversible) ───────────────────────────────────
if [ "$PURGE_MEMORY" = 1 ]; then
  hr "Purge memory"
  printf '  \033[31m[warn]\033[0m ⚠️  This will PERMANENTLY DELETE all agent memory:\n'
  printf '         memory/   MEMORY.md\n'
  printf '         This cannot be undone.\n'
  read -r -p "  Type 'wipe memory' to confirm: " confirm
  if [ "$confirm" = "wipe memory" ]; then
    [ -d "$WS/memory" ]    && rm -rf "$WS/memory"    && ok "removed memory/"
    [ -f "$WS/MEMORY.md" ] && rm "$WS/MEMORY.md"     && ok "removed MEMORY.md"
  else
    skip "purge-memory cancelled"
  fi
fi

echo; hr "done"
echo "  Run 'openclaw gateway restart' to apply config changes."
