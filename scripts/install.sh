#!/usr/bin/env bash
# dinomem — install script
# Sets up Dino Agent Memory System for an OpenClaw agent.
# Idempotent: safe to run multiple times.
#
# Usage:
#   bash scripts/install.sh [--workspace DIR] [--agent-id ID] [--no-docker] [--no-cron] [--no-backup-cron] [--force]
#
# Options:
#   --workspace DIR   Path to agent workspace (default: $OPENCLAW_WORKSPACE or ~/.openclaw/workspace)
#   --agent-id ID     OpenClaw agent ID (default: detected from workspace name)
#   --no-docker       Skip TEI Docker setup
#   --no-cron         Skip crontab registration
#   --no-backup-cron  Skip weekly backup cron (if you have your own backup system)
#   --force           Overwrite existing files
set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WS="${OPENCLAW_WORKSPACE:-$HOME/.openclaw/workspace}"
AGENT_ID=""
DO_DOCKER=1
DO_CRON=1
DO_BACKUP_CRON=1
FORCE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --workspace)  WS="$2"; shift 2 ;;
    --agent-id)   AGENT_ID="$2"; shift 2 ;;
    --no-docker)  DO_DOCKER=0; shift ;;
    --no-cron)         DO_CRON=0; shift ;;
    --no-backup-cron)  DO_BACKUP_CRON=0; shift ;;
    --force)      FORCE=1; shift ;;
    -h|--help)    grep -E '^#( |$)' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

ok()   { printf '  \033[32m[ok]\033[0m   %s\n' "$*"; }
skip() { printf '  \033[33m[skip]\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m[warn]\033[0m %s\n' "$*"; }
fail() { printf '  \033[31m[fail]\033[0m %s\n' "$*"; exit 1; }
hr()   { printf '\033[1m== %s ==\033[0m\n' "$*"; }

[ -d "$WS" ] || fail "Workspace not found: $WS  (pass --workspace DIR)"

# Auto-detect agent ID from workspace directory name
if [ -z "$AGENT_ID" ]; then
  AGENT_ID="$(basename "$WS")"
  AGENT_ID="${AGENT_ID#workspace-}"  # strip "workspace-" prefix if present
fi

OPENCLAW_DIR="$(dirname "$WS")"
SESSIONS_DIR="$OPENCLAW_DIR/agents/$AGENT_ID/sessions"

echo
hr "dinomem -> $WS (agent: $AGENT_ID)"

# ── 0) Pre-flight compatibility checks ───────────────────────────────────────────
hr "Pre-flight checks"
# Existing vector DB
if [ -d "$WS/kb/vector_db" ] && [ "$(ls -A "$WS/kb/vector_db" 2>/dev/null)" ]; then
  warn "kb/vector_db/ already exists and is not empty — dinomem will write to this path."
  warn "If this belongs to another system, back it up first or use a different workspace."
else
  ok "kb/vector_db/ clear"
fi
# Existing AGENTS.md memory block
if [ -f "$WS/AGENTS.md" ] && grep -qF "memory_recall" "$WS/AGENTS.md" 2>/dev/null; then
  warn "AGENTS.md already has a memory_recall section — dinomem block will be appended. Check for duplicates after install."
fi
# Root files size check (per-file + total)
ROOT_FILES="AGENTS.md SOUL.md IDENTITY.md TOOLS.md USER.md"
TOTAL_CHARS=0
for rf in $ROOT_FILES; do
  if [ -f "$WS/$rf" ]; then
    RF_SIZE=$(wc -c < "$WS/$rf")
    TOTAL_CHARS=$((TOTAL_CHARS + RF_SIZE))
    if [ "$RF_SIZE" -gt 20000 ]; then
      warn "$rf is ${RF_SIZE} chars — exceeds maxBootstrapFileChars (20000). Content beyond limit won't be injected."
      warn "  Trim $rf: remove outdated or redundant sections to keep it lightweight."
    elif [ "$RF_SIZE" -gt 15000 ]; then
      warn "$rf is ${RF_SIZE} chars — approaching maxBootstrapFileChars (20000). Consider trimming soon."
    fi
  fi
done
if [ "$TOTAL_CHARS" -gt 60000 ]; then
  warn "Total root files: ${TOTAL_CHARS} chars — exceeds maxBootstrapTotalChars (60000). Some files won't be fully injected."
  warn "  Check sizes: wc -c *.md — trim the largest files, remove outdated sections."
elif [ "$TOTAL_CHARS" -gt 50000 ]; then
  warn "Total root files: ${TOTAL_CHARS} chars — approaching maxBootstrapTotalChars (60000). Consider trimming soon."
else
  ok "Root files: ${TOTAL_CHARS} chars total — within limits"
fi

# ── 1) Create workspace directories ──────────────────────────────────────────
hr "Directories"
for d in procedures tools logs memory .memory_archive; do
  if [ -d "$WS/$d" ]; then skip "$d/ (exists)"; else mkdir -p "$WS/$d"; ok "$d/"; fi
done

# ── 2) Copy scripts ───────────────────────────────────────────────────────────
hr "Copying scripts"
for f in procedures/session_reset.py procedures/auto_session_reset.py procedures/extract_memory.py procedures/workspace_backup.py; do
  dst="$WS/$f"
  if [ -f "$dst" ] && [ "$FORCE" = 0 ]; then
    skip "$f (exists, use --force to overwrite)"
  else
    cp "$SKILL_DIR/$f" "$dst"
    sed -i "s|DINOMEM_WORKSPACE_PLACEHOLDER|$WS|g" "$dst"
    sed -i "s|DINOMEM_AGENT_SESSIONS_PLACEHOLDER|$SESSIONS_DIR|g" "$dst"
    sed -i "s|DINOMEM_AGENT_ID_PLACEHOLDER|$AGENT_ID|g" "$dst"
    ok "$f"
  fi
done

for f in procedures/memory_cleanup.py procedures/memory_review.py; do
  dst="$WS/$f"
  if [ -f "$dst" ] && [ "$FORCE" = 0 ]; then
    skip "$f (exists, use --force to overwrite)"
  else
    cp "$SKILL_DIR/$f" "$dst"
    sed -i "s|DINOMEM_WORKSPACE_PLACEHOLDER|$WS|g" "$dst"
    sed -i "s|DINOMEM_AGENT_SESSIONS_PLACEHOLDER|$SESSIONS_DIR|g" "$dst"
    sed -i "s|DINOMEM_AGENT_ID_PLACEHOLDER|$AGENT_ID|g" "$dst"
    ok "$f"
  fi
done

# ── 3) TEI Docker setup ───────────────────────────────────────────────────────
if [ "$DO_DOCKER" = 1 ]; then
  hr "TEI Embedding Server (Docker)"
  if ! command -v docker >/dev/null 2>&1; then
    warn "Docker not found — skipping TEI setup. Install Docker and re-run."
  elif lsof -i :8080 >/dev/null 2>&1 || ss -tlnp 2>/dev/null | grep -q ':8080 '; then
    warn "Port 8080 already in use — TEI not started. Check: lsof -i :8080"
    warn "Use --no-docker to skip TEI, or free port 8080 and re-run."
  else
    # Detect Compose plugin; fallback to docker run
    if docker compose version >/dev/null 2>&1; then
      cp "$SKILL_DIR/docker/docker-compose.tei.yml" "$WS/docker-compose.tei.yml"
      ok "docker-compose.tei.yml copied"
      if docker compose -f "$WS/docker-compose.tei.yml" ps 2>/dev/null | grep -q "running"; then
        skip "TEI container already running"
      else
        docker compose -f "$WS/docker-compose.tei.yml" up -d
        ok "TEI container started on port 8080 (compose)"
      fi
    else
      warn "docker compose plugin not found — using docker run fallback"
      if docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^tei-embed$'; then
        skip "TEI container already running (tei-embed)"
      else
        docker run -d --name tei-embed --restart unless-stopped \
          -p 8080:80 \
          ghcr.io/huggingface/text-embeddings-inference:cpu-1.6 \
          --model-id sentence-transformers/all-MiniLM-L6-v2 --auto-truncate
        ok "TEI container started on port 8080 (docker run)"
      fi
    fi
  fi
fi

# ── 4) Register cron jobs ─────────────────────────────────────────────────────
# upsert_cron: add or update a cron entry by script keyword
# Usage: upsert_cron <keyword> <comment> <cron_line> <label>
upsert_cron() {
  local keyword="$1" comment="$2" cron_line="$3" label="$4"
  local existing
  existing=$(crontab -l 2>/dev/null | grep "$keyword" || true)
  if [ "$existing" = "$cron_line" ]; then
    skip "$label (exists, up to date)"
  elif [ -n "$existing" ]; then
    # Content differs — replace
    { crontab -l 2>/dev/null | grep -v "$keyword"; echo "# $comment"; echo "$cron_line"; } | crontab -
    ok "$label (updated)"
  else
    { crontab -l 2>/dev/null; echo "# $comment"; echo "$cron_line"; } | crontab -
    ok "$label (registered)"
  fi
}

if [ "$DO_CRON" = 1 ]; then
  hr "Cron jobs"

  # auto_session_reset — every 15 min (orchestrates session archive + memory extraction)
  RESET_CRON="*/15 * * * * cd $WS && python3 procedures/auto_session_reset.py >> logs/auto_reset.log 2>&1"
  upsert_cron "auto_session_reset.py" "dinomem: auto session reset + memory extraction" "$RESET_CRON" "auto_session_reset cron (every 15 min)"

  # workspace_backup — weekly Sunday at 2:00 UTC (snapshot of memory + config files)
  if [ "$DO_BACKUP_CRON" = 1 ]; then
    BACKUP_CRON="0 2 * * 0 cd $WS && python3 procedures/workspace_backup.py >> logs/workspace_backup.log 2>&1"
    upsert_cron "workspace_backup.py" "dinomem: weekly workspace snapshot (keep 3)" "$BACKUP_CRON" "workspace_backup cron (weekly Sunday 2:00 UTC)"
  else
    skip "workspace_backup cron (--no-backup-cron)"
  fi

  # memory_cleanup — daily at 5:00 UTC
  CLEANUP_CRON="0 5 * * * cd $WS && python3 procedures/memory_cleanup.py >> logs/memory_cleanup.log 2>&1"
  upsert_cron "memory_cleanup.py" "dinomem: daily memory deduplication" "$CLEANUP_CRON" "memory_cleanup cron (daily 5:00 UTC)"

  # memory_review — daily at 5:30 UTC (batched, full cycle ~7 days)
  REVIEW_CRON="30 5 * * * cd $WS && python3 procedures/memory_review.py >> logs/memory_review.log 2>&1"
  upsert_cron "memory_review.py" "dinomem: daily batched memory review (LLM)" "$REVIEW_CRON" "memory_review cron (daily 5:30 UTC, batched)"

  # note_review — daily via OpenClaw cron (LLM judges resolved _note_*.md and deletes them)
  # Registered via OpenClaw cron API, not crontab
  NOTE_REVIEW_CHECK=$(python3 -c "
import subprocess, json, sys
try:
    r = subprocess.run(['openclaw', 'cron', 'list', '--json'], capture_output=True, text=True, timeout=10)
    jobs = json.loads(r.stdout) if r.returncode == 0 else []
    exists = any('note' in j.get('name','').lower() and 'review' in j.get('name','').lower() for j in (jobs if isinstance(jobs, list) else jobs.get('jobs', {}).get('jobs', [])))
    print('exists' if exists else 'missing')
except: print('skip')
" 2>/dev/null)
  if [ "$NOTE_REVIEW_CHECK" = "exists" ]; then
    skip "note_review OpenClaw cron (exists)"
  elif [ "$NOTE_REVIEW_CHECK" = "skip" ]; then
    warn "Could not check OpenClaw cron — add Daily Note Review cron manually via OpenClaw"
  else
    python3 - <<PYEOF
import subprocess, json
job = {
    "name": "Daily Note Review",
    "schedule": {"kind": "cron", "expr": "0 6 * * *", "tz": "UTC"},
    "payload": {
        "kind": "agentTurn",
        "message": "Scan all memory/_note_*.md files in $WS/memory/. For each file, check if the task/todo described is already completed based on workspace state (check if relevant files exist, features built, etc). If resolved: delete the _note_*.md file. If still pending: leave it.",
        "timeoutSeconds": 120
    },
    "sessionTarget": "isolated",
    "delivery": {"mode": "none"}
}
r = subprocess.run(['openclaw', 'cron', 'add', '--json', json.dumps(job)], capture_output=True, text=True, timeout=15)
if r.returncode == 0:
    print("  \033[32m[ok]\033[0m   note_review OpenClaw cron registered (daily 6:00 UTC)")
else:
    print(f"  \033[33m[warn]\033[0m Could not register note_review cron: {r.stderr[:100]}")
PYEOF
  fi

  # TEI @reboot
  if [ "$DO_DOCKER" = 1 ] && command -v docker >/dev/null 2>&1; then
    if docker compose version >/dev/null 2>&1; then
    TEI_CRON="@reboot sleep 30 && docker compose -f $WS/docker-compose.tei.yml up -d >> /tmp/tei-startup.log 2>&1"
  else
    TEI_CRON="@reboot sleep 30 && docker start tei-embed >> /tmp/tei-startup.log 2>&1"
  fi
  TEI_CRON="$TEI_CRON" # assigned above
    upsert_cron "docker-compose.tei.yml" "dinomem: TEI auto-start on reboot" "$TEI_CRON" "TEI @reboot cron"
  fi
fi

# ── 5) Patch openclaw.json config ─────────────────────────────────────────────
hr "OpenClaw config"
OPENCLAW_JSON="${OPENCLAW_JSON:-$HOME/.openclaw/openclaw.json}"
[ -f "$OPENCLAW_JSON" ] || OPENCLAW_JSON="$OPENCLAW_DIR/openclaw.json"
if [ ! -f "$OPENCLAW_JSON" ]; then
  warn "openclaw.json not found at $OPENCLAW_JSON — skipping config patch"
else
  python3 - <<PYEOF
import json, sys

path = "$OPENCLAW_JSON"
with open(path) as f:
    cfg = json.load(f)

changed = []

# session.reset -> idle 7 days (skip if user already has custom idle config)
session = cfg.setdefault("session", {})
reset = session.setdefault("reset", {})
if reset.get("mode") not in (None, "idle"):
    print(f"  \033[33m[warn]\033[0m session.reset.mode is '{reset.get('mode')}' — skipping (dinomem needs idle mode; set manually if needed)")
elif reset.get("idleMinutes") and reset.get("idleMinutes") != 10080:
    print(f"  \033[33m[warn]\033[0m session.reset.idleMinutes is {reset.get('idleMinutes')} (custom) — keeping existing value")
    reset["mode"] = "idle"  # ensure mode is set even if minutes kept
else:
    reset["mode"] = "idle"
    reset["idleMinutes"] = 10080
    changed.append("session.reset -> idle 7 days")

agents = cfg.setdefault("agents", {})
defaults = agents.setdefault("defaults", {})

# contextPruning -> off (let compaction handle context, not TTL-based blunt pruning)
pruning = defaults.setdefault("contextPruning", {})
if pruning.get("mode") != "off":
    defaults["contextPruning"] = {"mode": "off"}
    changed.append("contextPruning.mode -> off (compaction handles context)")

# compaction -> safeguard with recommended settings
compaction = defaults.setdefault("compaction", {})
# Only patch mode and memoryFlush — leave reserveTokens/keepRecentTokens to OpenClaw defaults.
# reserveTokens default (16384) + floor (20000) are model-agnostic.
# Hardcoding 50k would break small context window models (8k/32k).
compaction_patch = {
    "mode": "safeguard",
    "truncateAfterCompaction": True,
    "memoryFlush": {"enabled": False, "softThresholdTokens": 10000},
}
needs_update = any(compaction.get(k) != v for k, v in compaction_patch.items())
if needs_update:
    compaction.update(compaction_patch)
    changed.append("compaction -> safeguard mode (reserveTokens/keepRecentTokens left to OpenClaw defaults)")

# workspaceBootstrap -> always (root files injected every turn, not skipped on continuation)
if defaults.get("workspaceBootstrap") not in (None, "always"):
    defaults["workspaceBootstrap"] = "always"
    changed.append("workspaceBootstrap -> always (root files injected every turn)")

# startupContext.enabled -> false (use memory_search pull instead of startup push-injection)
startup_ctx = defaults.setdefault("startupContext", {})
if startup_ctx.get("enabled") is not False:
    startup_ctx["enabled"] = False
    changed.append("startupContext.enabled -> false (memory_search pull is more precise than startup push)")

# memorySearch -> TEI openai-compatible (skip if user already has custom provider)
mem_search = defaults.get("memorySearch", {})
existing_provider = mem_search.get("provider")
if existing_provider and existing_provider not in (None, "openai-compatible", "built-in"):
    print(f"  \033[33m[warn]\033[0m memorySearch.provider is '{existing_provider}' (custom) — skipping. dinomem TEI won't be wired automatically. Set manually if needed.")
elif mem_search.get("provider") != "openai-compatible":
    defaults["memorySearch"] = {
        "provider": "openai-compatible",
        "model": "sentence-transformers/all-MiniLM-L6-v2",
        "remote": {"baseUrl": "http://localhost:8080/v1"},
        "query": {"hybrid": {"vectorWeight": 0.7, "textWeight": 0.3}},
    }
    changed.append("memorySearch -> TEI openai-compatible (localhost:8080)")

# models.providers -> add tei-embed provider
providers = cfg.setdefault("models", {}).setdefault("providers", {})
if "tei-embed" not in providers:
    providers["tei-embed"] = {
        "api": "openai-completions",
        "baseUrl": "http://localhost:8080/v1",
        "apiKey": "dummy",
        "models": [{"id": "sentence-transformers/all-MiniLM-L6-v2", "name": "sentence-transformers/all-MiniLM-L6-v2"}],
    }
    changed.append("models.providers.tei-embed -> added")

if changed:
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)
    for c in changed:
        print(f"  \033[32m[ok]\033[0m   patched: {c}")
    print("  \033[33m[warn]\033[0m Restart OpenClaw: openclaw gateway restart")
else:
    print("  \033[33m[skip]\033[0m openclaw.json already configured")
PYEOF
fi

# ── 6) Wire AGENTS.md ─────────────────────────────────────────────────────────
hr "AGENTS.md"
AGENTS="$WS/AGENTS.md"
BEGIN="<!-- BEGIN:dinomem (managed — do not edit between markers) -->"
END="<!-- END:dinomem -->"
BLOCK="$BEGIN
## dinomem
  memory_index: {file: MEMORY.md, instruction: topic in MEMORY.md → memory_search then memory_get}
  constraints:
    M0: context_unclear → memory_search + memory_get; fallback: ask
    M1: before tool/script with side effects → memory_search first
    M2:
      when: named entity | temporal ref | implicit ref | continuation request
      action: rewrite implicit query → memory_search FIRST (before fs/exec/any tool)
      enforce: no exceptions; memory before filesystem; violating M2 = repeating mistakes

  memory_pin:
    trigger: permanent_fact OR user emphasizes importance (any language)
    uncertain: ask user before pinning
    long_docs: docs/<slug>.md → docs_ingest.py
    permanent: {prefix: _pin_, location: memory/, format: "# Title\n\n<content>", slug: "lowercase-hyphens-max30"}
    transient:
      trigger: todo/reminder/planned task/time-bound
      uncertain: ask user before noting
      prefix: _note_
      location: memory/
      format: '# Title\nstatus: pending\ndate: YYYY-MM-DD\ntime: HH:MM\n<content>'
      slug: "lowercase-hyphens-max30"

  memory_recall:
    use: topic in MEMORY.md | context unclear | prior decisions/prefs relevant
    after_search: memory_get on relevant result
    skip: do not call memory_search every turn

  self_config:
    tool: tools/config_tool.py
    trigger: user implies changing behavior/rules/workflows/persona/tools/preferences (SOUL/IDENTITY/AGENTS/TOOLS/USER)
    rule: classify intent → select target → generate content → call config_tool.py
    routing:
      SOUL.md: [tone,verbosity,style,personality]
      IDENTITY.md: [name,role,persona]
      AGENTS.md: [sop,rule,workflow,constraint,when_to_use]
      TOOLS.md: [new_tool,script_spec,capability]
      USER.md: [user_pref,user_context,user_info]
      docs/<slug>.md: [long_doc,contract,book,legal] → docs_ingest.py
    removal: user says remove/stop/delete → call remove(section_key); confirm first
    confirm_before_write: [SOUL.md, IDENTITY.md, AGENTS.md]
    skip_confirm: [TOOLS.md, USER.md]
    ambiguous: ask one question then route

## backup_restore
  when: restore request | "what backups" | undo file/memory change
  tool: procedures/workspace_backup.py
  list: python3 procedures/workspace_backup.py --list
  restore: python3 procedures/workspace_backup.py --restore [index|name] [--yes]
  restore_file: python3 procedures/workspace_backup.py --restore [index|name] --file <path>
  note: auto-runs via cron
$END"

touch "$AGENTS"
if grep -qF "$BEGIN" "$AGENTS" 2>/dev/null; then
  skip "AGENTS.md already wired (use --force to refresh)"
else
  printf '\n%s\n' "$BLOCK" >> "$AGENTS"
  ok "AGENTS.md wired"
fi

# ── 6b) Wire TOOLS.md ────────────────────────────────────────────────────────
hr "TOOLS.md"
TOOLS="$WS/TOOLS.md"
TOOLS_MARKER="# dinomem: workspace_backup"
TOOLS_BLOCK="$TOOLS_MARKER
  workspace_backup:
    path: procedures/workspace_backup.py
    type: exec
    capabilities:
      - full_workspace_snapshot
      - list_backups
      - restore_all
      - restore_single_file
    inputs:
      cmd:
        type: enum
        values: ['(none)', '--list', '--restore', '--restore --file PATH', '--restore --yes']
      target:
        type: string
        required: false
        note: 'Snapshot name or index from --list. Default: latest.'
      file:
        type: string
        required: false
        note: 'Relative path to restore single file e.g. memory/2026-06-01.md'
    output:
      type: text
    constraints:
      mode: read_write"

if grep -qF "$TOOLS_MARKER" "$TOOLS" 2>/dev/null; then
  skip "TOOLS.md already has workspace_backup entry"
else
  printf '\n%s\n' "$TOOLS_BLOCK" >> "$TOOLS"
  ok "TOOLS.md wired (workspace_backup)"
fi

# ── 7) Verify tools allowlist ─────────────────────────────────────────────────
hr "Tools allowlist"
python3 - <<PYEOF
import json

path = "$OPENCLAW_DIR/openclaw.json"
try:
    cfg = json.load(open(path))
    agents_list = cfg.get("agents", {}).get("list", [])
    agent = next((a for a in agents_list if a.get("id") == "$AGENT_ID"), None)
    if agent:
        tools_allow = agent.get("tools", {}).get("allow", [])
        missing = [t for t in ["memory_search", "memory_get"] if t not in tools_allow]
        if missing:
            print(f"  \033[33m[warn]\033[0m Agent '$AGENT_ID' tools.allow is missing: {missing}")
            print(f"  \033[33m[warn]\033[0m Add these to agents.list[$AGENT_ID].tools.allow in openclaw.json")
        else:
            print(f"  \033[32m[ok]\033[0m   memory_search + memory_get in tools.allow")
    else:
        print(f"  \033[33m[warn]\033[0m Agent '$AGENT_ID' not found in agents.list — add memory_search + memory_get to tools.allow manually")
except Exception as e:
    print(f"  \033[33m[warn]\033[0m Could not check tools.allow: {e}")
PYEOF

echo
hr "done"
echo "  dinomem installed for agent: $AGENT_ID"
echo "  workspace: $WS"
echo ""
echo "  Next steps:"
echo "  1. Restart OpenClaw:  openclaw gateway restart"
echo "  2. Verify TEI:        curl http://localhost:8080/health"
echo "  3. Add to tools.allow in openclaw.json: memory_search, memory_get"
echo "  4. First extraction:  python3 $WS/procedures/auto_session_reset.py"
echo ""
echo ""
echo "  ⚠️  ACTION REQUIRED: Compaction tuning"
echo "  Skipping this hurts memory quality and response performance."
echo "  Takes 2 minutes — do this before restarting OpenClaw."
echo "  Guide: https://github.com/02-dino/dinomem#compaction-tuning-manual-strongly-recommended"
echo ""
echo "  Undo: bash $SKILL_DIR/scripts/uninstall.sh --workspace $WS --agent-id $AGENT_ID"
