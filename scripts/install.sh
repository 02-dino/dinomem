#!/usr/bin/env bash
# dinomem — install script
# Sets up Dino Agent Memory System for an OpenClaw agent.
# Idempotent: safe to run multiple times.
#
# Usage:
#   bash scripts/install.sh [--workspace DIR] [--agent-id ID] [--no-docker] [--no-cron] [--no-backup-cron] [--no-smart-cache] [--force] [--dry-run]
#
# Options:
#   --workspace DIR   Path to agent workspace (default: $OPENCLAW_WORKSPACE or ~/.openclaw/workspace)
#   --agent-id ID     OpenClaw agent ID (default: detected from workspace name)
#   --no-docker       ADVANCED. Skip the TEI Docker embed server. ONLY valid if you
#                     already serve a TEI-compatible /v1/embeddings endpoint yourself
#                     (native binary, remote host, other container). Point the engine
#                     at it with DINOMEM_EMBED_URL=<url> (default http://localhost:8080
#                     /v1/embeddings). Without an embed server, memory extraction/review
#                     cannot embed and the engine is non-functional.
#   --no-cron         ADVANCED. Skip crontab registration. Cron is what DRIVES dinomem
#                     (extraction, review, cleanup, session reset all run as cron jobs).
#                     A fresh install with --no-cron copies files but NEVER RUNS itself.
#                     Use only for (1) re-runs/upgrades where crons already exist, or
#                     (2) wiring the jobs via your own scheduler (systemd timers, etc.).
#   --no-backup-cron  Skip weekly backup cron (if you have your own backup system)
#   --no-smart-cache  Skip bundling the smart-cache-pro (compression-only) plugin
#   --force           Overwrite existing files
#   --dry-run         Preview every change without writing anything (no files,
#                     no crons, no Docker, no config patch). Idempotency-aware:
#                     reports would-create vs already-present.
set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WS="${OPENCLAW_WORKSPACE:-$HOME/.openclaw/workspace}"
AGENT_ID=""
DO_DOCKER=1
DO_CRON=1
DO_BACKUP_CRON=1
DO_SMART_CACHE=1
FORCE=0
DRY_RUN=0

# smart-cache-pro (compression-only) — bundled token-discipline plugin. Overridable.
SMART_CACHE_REPO="${SMART_CACHE_REPO:-https://github.com/02-dino/smart-cache-pro}"
SMART_CACHE_BRANCH="${SMART_CACHE_BRANCH:-feat/compression-only-generalized}"

while [ $# -gt 0 ]; do
  case "$1" in
    --workspace)  WS="$2"; shift 2 ;;
    --agent-id)   AGENT_ID="$2"; shift 2 ;;
    --no-docker)  DO_DOCKER=0; shift ;;
    --no-cron)         DO_CRON=0; shift ;;
    --no-backup-cron)  DO_BACKUP_CRON=0; shift ;;
    --no-smart-cache)  DO_SMART_CACHE=0; shift ;;
    --force)      FORCE=1; shift ;;
    --dry-run)    DRY_RUN=1; shift ;;
    --agree)      shift ;;  # no-op: base has no license gate; neuron passes this through after the human accepted the neuron license. Accept+ignore so neuron auto-base install doesn't die on 'unknown arg'.
    -h|--help)    grep -E '^#( |$)' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

ok()   { printf '  \033[32m[ok]\033[0m   %s\n' "$*"; }
skip() { printf '  \033[33m[skip]\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m[warn]\033[0m %s\n' "$*"; }
fail() { printf '  \033[31m[fail]\033[0m %s\n' "$*"; exit 1; }
hr()   { printf '\033[1m== %s ==\033[0m\n' "$*"; }
# plan: in --dry-run, print what WOULD happen instead of doing it.
plan() { printf '  \033[36m[plan]\033[0m %s\n' "$*"; }
# run: execute a command, or in --dry-run print it (with an optional label).
# Usage: run "<human label>" <command> [args...]
run() {
  local label="$1"; shift
  if [ "$DRY_RUN" = 1 ]; then
    plan "$label"
  else
    "$@"
  fi
}

# tei_healthy: return 0 if something on :8080 answers TEI's /health (200).
# Lets us treat an already-running healthy TEI as reusable instead of a hard
# port collision. TEI serves /health on its listen port; doctor.sh uses the same probe.
tei_healthy() {
  local url="http://localhost:${TEI_PORT:-8080}/health"
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "$url" 2>/dev/null)"
  [ "$code" = "200" ]
}

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
if [ "$DRY_RUN" = 1 ]; then
  printf '\033[1;36m== DRY RUN — preview only, nothing will be written ==\033[0m\n'
fi

# ── 0) Pre-flight compatibility checks ───────────────────────────────────────────
hr "Pre-flight checks"
# Python version check
if ! command -v python3 &>/dev/null; then
  warn "python3 not found — attempting install..."
  if command -v brew &>/dev/null; then
    brew install python3 && ok "python3 installed (brew)" || warn "python3 install failed — install manually: https://python.org"
  elif command -v apt-get &>/dev/null; then
    apt-get install -y software-properties-common 2>/dev/null
    add-apt-repository -y ppa:deadsnakes/ppa 2>/dev/null || true
    apt-get update -q && apt-get install -y python3.12 python3.12-venv python3-pip \
      && ln -sf /usr/bin/python3.12 /usr/local/bin/python3 \
      && ok "python3.12 installed (deadsnakes)" \
      || warn "python3 install failed — install manually: https://python.org"
  elif command -v curl &>/dev/null; then
    curl https://pyenv.run | bash \
      && export PATH="$HOME/.pyenv/bin:$PATH" \
      && pyenv install 3.12 && pyenv global 3.12 \
      && ok "python3.12 installed (pyenv)" \
      || warn "pyenv install failed — install python3 manually: https://python.org"
  else
    warn "No package manager found — install python3 manually: https://python.org"
  fi
fi
PY_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || echo "0.0")
PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 8 ]; }; then
  warn "Python $PY_VERSION detected — dinomem requires Python 3.8+. Upgrade before continuing."
else
  ok "Python $PY_VERSION"
fi
# ── System resource check (RAM/CPU warn, disk block-unless-force) ─────────────
# Minimum spec (inferred from footprint; TEI CPU embed server is the driver):
#   dinomem base : 2 vCPU / 2 GB RAM / 5 GB free disk
# RAM/CPU below-min => warn + continue (TEI may OOM under batch load).
# Disk below hard floor (2 GB) => block unless --force (image pull WILL fail mid-install).
MIN_RAM_MB=2048
MIN_CPU=2
DISK_HARD_MIN_MB=2048   # hard floor: below this the TEI image pull cannot complete
DISK_REC_MB=5120        # recommended free
PREFLIGHT_WARN=""       # accumulator: machine-readable below-spec signal for agent-driven installs
# Total RAM
if [ "$(uname)" = "Darwin" ]; then
  TOTAL_RAM_MB=$(( $(sysctl -n hw.memsize 2>/dev/null || echo 0) / 1024 / 1024 ))
  CPU_COUNT=$(sysctl -n hw.ncpu 2>/dev/null || echo 0)
  DISK_FREE_MB=$(df -m "$WS" 2>/dev/null | awk 'NR==2{print $4}')
else
  TOTAL_RAM_MB=$(( $(awk '/MemTotal/{print $2}' /proc/meminfo 2>/dev/null || echo 0) / 1024 ))
  CPU_COUNT=$(nproc 2>/dev/null || echo 0)
  DISK_FREE_MB=$(df -m "$WS" 2>/dev/null | awk 'NR==2{print $4}')
fi
[ -z "$DISK_FREE_MB" ] && DISK_FREE_MB=0
# RAM (warn only)
if [ "$TOTAL_RAM_MB" -gt 0 ] && [ "$TOTAL_RAM_MB" -lt "$MIN_RAM_MB" ]; then
  warn "RAM ${TOTAL_RAM_MB}MB is below recommended ${MIN_RAM_MB}MB — TEI embed server may OOM under batch load. Continuing."
  PREFLIGHT_WARN="${PREFLIGHT_WARN}ram=${TOTAL_RAM_MB}MB<${MIN_RAM_MB}MB "
elif [ "$TOTAL_RAM_MB" -gt 0 ]; then
  ok "RAM ${TOTAL_RAM_MB}MB"
else
  warn "Could not detect RAM — ensure >= ${MIN_RAM_MB}MB before running TEI."
fi
# CPU (warn only)
if [ "$CPU_COUNT" -gt 0 ] && [ "$CPU_COUNT" -lt "$MIN_CPU" ]; then
  warn "CPU ${CPU_COUNT} core(s) is below recommended ${MIN_CPU} — embedding will be slow. Continuing."
  PREFLIGHT_WARN="${PREFLIGHT_WARN}cpu=${CPU_COUNT}<${MIN_CPU} "
elif [ "$CPU_COUNT" -gt 0 ]; then
  ok "CPU ${CPU_COUNT} core(s)"
fi
# Disk (block unless --force below hard floor; warn between hard floor and recommended)
if [ "$DISK_FREE_MB" -lt "$DISK_HARD_MIN_MB" ]; then
  if [ "$FORCE" = 1 ]; then
    warn "Free disk ${DISK_FREE_MB}MB is below hard floor ${DISK_HARD_MIN_MB}MB — --force set, continuing anyway (TEI image pull may fail)."
  else
    printf 'DINOMEM_PREFLIGHT_BLOCK=disk=%sMB<%sMB\n' "$DISK_FREE_MB" "$DISK_HARD_MIN_MB"
    fail "Free disk ${DISK_FREE_MB}MB is below hard floor ${DISK_HARD_MIN_MB}MB — TEI image pull will fail mid-install. Free space or re-run with --force. (Agent installers: this exit 1 is a hard stop; report to user, do NOT auto-retry with --force.)"
  fi
elif [ "$DISK_FREE_MB" -lt "$DISK_REC_MB" ]; then
  warn "Free disk ${DISK_FREE_MB}MB is below recommended ${DISK_REC_MB}MB — tight but should fit. Continuing."
  PREFLIGHT_WARN="${PREFLIGHT_WARN}disk=${DISK_FREE_MB}MB<${DISK_REC_MB}MB "
else
  ok "Free disk ${DISK_FREE_MB}MB"
fi
# Agent-installer contract: emit a machine-readable, hard-to-skim signal so an
# automated caller cannot silently blur past below-spec warnings. Humans get the
# colored [warn] lines above; agents get this token + explicit instruction.
if [ -n "$PREFLIGHT_WARN" ]; then
  printf '\033[1;33m[AGENT-NOTICE]\033[0m below-recommended spec detected. If this install is agent-driven, surface this to the user before continuing (do NOT auto-pass --force):\n'
  printf 'DINOMEM_PREFLIGHT_WARN=%s\n' "$PREFLIGHT_WARN"
fi
# Workspace writable check
if [ ! -d "$WS" ]; then
  warn "Workspace '$WS' does not exist — create it first or pass correct --workspace path."
elif [ ! -w "$WS" ]; then
  warn "Workspace '$WS' is not writable — fix permissions before installing."
else
  ok "Workspace writable: $WS"
fi
# OpenClaw running check
if command -v openclaw >/dev/null 2>&1 && openclaw status >/dev/null 2>&1; then
  ok "OpenClaw running"
else
  warn "OpenClaw not running or not found — config patches will be skipped. Start OpenClaw and re-run."
fi
# openclaw.json exists
OPENCLAW_JSON="${OPENCLAW_CONFIG:-$HOME/.openclaw/openclaw.json}"
if [ -f "$OPENCLAW_JSON" ]; then
  ok "openclaw.json found ($OPENCLAW_JSON)"
else
  warn "openclaw.json not found at $OPENCLAW_JSON — config patches will be skipped. Set OPENCLAW_CONFIG or ensure OpenClaw is initialized."
fi
# Port 8080 conflict — but a healthy existing TEI on 8080 is reusable, not a conflict.
if lsof -i :8080 >/dev/null 2>&1; then
  if tei_healthy; then
    ok "Port 8080 in use by a healthy TEI (/health 200) — will reuse it, not start a new one."
    TEI_REUSE=1
  else
    warn "Port 8080 already in use by something that is NOT a healthy TEI — embedding server may not start. Check: lsof -i :8080"
  fi
else
  ok "Port 8080 free"
fi
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
      warn "$rf is ${RF_SIZE} chars — getting large. Consider trimming soon."
    elif [ "$RF_SIZE" -gt 10000 ]; then
      warn "$rf is ${RF_SIZE} chars — approaching 15k. Keep an eye on size."
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
  if [ -d "$WS/$d" ]; then skip "$d/ (exists)"; elif [ "$DRY_RUN" = 1 ]; then plan "create dir $d/"; else mkdir -p "$WS/$d"; ok "$d/"; fi
done

# ── 2) Copy scripts ───────────────────────────────────────────────────────────
hr "Copying scripts"
for f in procedures/session_reset.py procedures/auto_session_reset.py procedures/extract_memory.py procedures/workspace_backup.py; do
  dst="$WS/$f"
  if [ -f "$dst" ] && [ "$FORCE" = 0 ]; then
    skip "$f (exists, use --force to overwrite)"
  elif [ "$DRY_RUN" = 1 ]; then
    plan "copy + substitute placeholders -> $f"
  else
    cp "$SKILL_DIR/$f" "$dst"
    sed -i "s|DINOMEM_WORKSPACE_PLACEHOLDER|$WS|g" "$dst"
    sed -i "s|DINOMEM_AGENT_SESSIONS_PLACEHOLDER|$SESSIONS_DIR|g" "$dst"
    sed -i "s|DINOMEM_AGENT_ID_PLACEHOLDER|$AGENT_ID|g" "$dst"
    ok "$f"
  fi
done

for f in procedures/memory_cleanup.py procedures/memory_review.py procedures/cleanup_startup_daily.py; do
  dst="$WS/$f"
  if [ -f "$dst" ] && [ "$FORCE" = 0 ]; then
    skip "$f (exists, use --force to overwrite)"
  elif [ "$DRY_RUN" = 1 ]; then
    plan "copy + substitute placeholders -> $f"
  else
    cp "$SKILL_DIR/$f" "$dst"
    sed -i "s|DINOMEM_WORKSPACE_PLACEHOLDER|$WS|g" "$dst"
    sed -i "s|DINOMEM_AGENT_SESSIONS_PLACEHOLDER|$SESSIONS_DIR|g" "$dst"
    sed -i "s|DINOMEM_AGENT_ID_PLACEHOLDER|$AGENT_ID|g" "$dst"
    ok "$f"
  fi
done

# ── 2b) Install reset-extract hook ──────────────────────────────────────────
hr "Reset-extract hook (0-delay memory pipeline on /new /reset)"
HOOK_SRC="$SKILL_DIR/hooks/dinomem-reset-extract"
HOOK_DST="$WS/hooks/dinomem-reset-extract"
if [ -d "$HOOK_DST" ] && [ "$FORCE" = 0 ]; then
  skip "hooks/dinomem-reset-extract/ (exists, use --force to overwrite)"
elif [ "$DRY_RUN" = 1 ]; then
  plan "copy hooks/dinomem-reset-extract/ -> $WS/hooks/"
  plan "openclaw hooks enable dinomem-reset-extract"
else
  mkdir -p "$WS/hooks"
  rm -rf "$HOOK_DST"
  cp -r "$HOOK_SRC" "$HOOK_DST"
  ok "hooks/dinomem-reset-extract/ copied"
  if command -v openclaw >/dev/null 2>&1 && openclaw status >/dev/null 2>&1; then
    openclaw hooks enable dinomem-reset-extract >/dev/null 2>&1 \
      && ok "dinomem-reset-extract hook enabled (restart OpenClaw to activate)" \
      || warn "openclaw hooks enable failed — run manually: openclaw hooks enable dinomem-reset-extract"
  else
    warn "OpenClaw not running — run after restart: openclaw hooks enable dinomem-reset-extract"
  fi
fi

# ── 2c) Install open-notes hook ────────────────────────────────────────────
hr "Open-notes hook (inject open _note_ manifest at bootstrap)"
HOOK2_SRC="$SKILL_DIR/hooks/dinomem-open-notes"
HOOK2_DST="$WS/hooks/dinomem-open-notes"
if [ -d "$HOOK2_DST" ] && [ "$FORCE" = 0 ]; then
  skip "hooks/dinomem-open-notes/ (exists, use --force to overwrite)"
elif [ "$DRY_RUN" = 1 ]; then
  plan "copy hooks/dinomem-open-notes/ -> $WS/hooks/"
  plan "openclaw hooks enable dinomem-open-notes"
else
  mkdir -p "$WS/hooks"
  rm -rf "$HOOK2_DST"
  cp -r "$HOOK2_SRC" "$HOOK2_DST"
  ok "hooks/dinomem-open-notes/ copied"
  if command -v openclaw >/dev/null 2>&1 && openclaw status >/dev/null 2>&1; then
    openclaw hooks enable dinomem-open-notes >/dev/null 2>&1 \
      && ok "dinomem-open-notes hook enabled (restart OpenClaw to activate)" \
      || warn "openclaw hooks enable failed — run manually: openclaw hooks enable dinomem-open-notes"
  else
    warn "OpenClaw not running — run after restart: openclaw hooks enable dinomem-open-notes"
  fi
fi

# ── 2d) Install skills ─────────────────────────────────────────────────────
hr "Skills (memory-pinning, backup-restore, self-config)"
if [ -d "$SKILL_DIR/skills" ]; then
  for _sk in "$SKILL_DIR/skills"/*/; do
    [ -d "$_sk" ] || continue
    _skname="$(basename "$_sk")"
    _skdst="$WS/skills/$_skname"
    if [ -d "$_skdst" ] && [ "$FORCE" = 0 ]; then
      skip "skills/$_skname/ (exists, use --force to overwrite)"
    elif [ "$DRY_RUN" = 1 ]; then
      plan "copy skills/$_skname/ -> $WS/skills/"
    else
      mkdir -p "$WS/skills"
      rm -rf "$_skdst"
      cp -r "$_sk" "$_skdst"
      ok "skills/$_skname/ copied"
    fi
  done
else
  skip "no skills/ in package"
fi

# ── 3) TEI Docker setup ────────────────────────────────────────────────────────
if [ "$DO_DOCKER" = 1 ]; then
  hr "TEI Embedding Server (Docker)"
  if ! command -v docker >/dev/null 2>&1; then
    warn "Docker not found — skipping TEI setup. Install Docker and re-run."
  elif [ "${TEI_REUSE:-0}" = 1 ] || tei_healthy; then
    ok "Existing healthy TEI already answering on :8080 (/health 200) — reusing it, not starting a new container."
  elif lsof -i :8080 >/dev/null 2>&1 || ss -tlnp 2>/dev/null | grep -q ':8080 '; then
    warn "Port 8080 in use by a non-TEI process — TEI not started. Check: lsof -i :8080"
    warn "Use --no-docker to skip TEI, or free port 8080 and re-run."
  else
    # Detect Compose plugin; fallback to docker run
    if docker compose version >/dev/null 2>&1; then
      run "copy docker-compose.tei.yml -> $WS/" cp "$SKILL_DIR/docker/docker-compose.tei.yml" "$WS/docker-compose.tei.yml"
      [ "$DRY_RUN" = 1 ] || ok "docker-compose.tei.yml copied"
      if [ "$DRY_RUN" != 1 ] && docker compose -f "$WS/docker-compose.tei.yml" ps 2>/dev/null | grep -q "running"; then
        skip "TEI container already running"
      else
        run "docker compose up -d (TEI embed server on :8080)" docker compose -f "$WS/docker-compose.tei.yml" up -d
        [ "$DRY_RUN" = 1 ] || ok "TEI container started on port 8080 (compose)"
      fi
    else
      warn "docker compose plugin not found — using docker run fallback"
      if [ "$DRY_RUN" != 1 ] && docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^tei-embed$'; then
        skip "TEI container already running (tei-embed)"
      else
        run "docker run tei-embed (TEI embed server on :8080)" docker run -d --name tei-embed --restart unless-stopped \
          -p 8080:80 \
          ghcr.io/huggingface/text-embeddings-inference:cpu-1.6 \
          --model-id intfloat/multilingual-e5-small --auto-truncate
        [ "$DRY_RUN" = 1 ] || ok "TEI container started on port 8080 (docker run)"
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
    if [ "$DRY_RUN" = 1 ]; then plan "update cron: $label"; return; fi
    # Content differs — replace
    { crontab -l 2>/dev/null | grep -v "$keyword"; echo "# $comment"; echo "$cron_line"; } | crontab -
    ok "$label (updated)"
  else
    if [ "$DRY_RUN" = 1 ]; then plan "register cron: $label"; return; fi
    { crontab -l 2>/dev/null; echo "# $comment"; echo "$cron_line"; } | crontab -
    ok "$label (registered)"
  fi
}

if [ "$DO_CRON" = 1 ]; then
  hr "Cron jobs"

  # Env prefix threaded into the embed-consuming crons so a remote/non-Docker
  # embedding endpoint set at install time (DINOMEM_EMBED_URL) actually reaches
  # cron-run scripts (crond does not inherit your interactive shell env).
  # Empty when unset → no change (default localhost:8080 baked into the scripts).
  EMBED_ENV=""
  if [ -n "${DINOMEM_EMBED_URL:-}" ]; then
    EMBED_ENV="DINOMEM_EMBED_URL=$DINOMEM_EMBED_URL "
    ok "cron embed endpoint: $DINOMEM_EMBED_URL"
  fi

  # auto_session_reset — every 15 min (orchestrates session archive + memory extraction)
  RESET_CRON="*/15 * * * * cd $WS && ${EMBED_ENV}python3 procedures/auto_session_reset.py >> logs/auto_reset.log 2>&1"
  upsert_cron "auto_session_reset.py" "dinomem: auto session reset + memory extraction" "$RESET_CRON" "auto_session_reset cron (every 15 min)"

  # workspace_backup — weekly Sunday at 2:00 UTC (snapshot of memory + config files)
  if [ "$DO_BACKUP_CRON" = 1 ]; then
    BACKUP_CRON="0 2 * * 0 cd $WS && python3 procedures/workspace_backup.py >> logs/workspace_backup.log 2>&1"
    upsert_cron "workspace_backup.py" "dinomem: weekly workspace snapshot (keep 3)" "$BACKUP_CRON" "workspace_backup cron (weekly Sunday 2:00 UTC)"
  else
    skip "workspace_backup cron (--no-backup-cron)"
  fi

  # memory_cleanup — daily at 5:00 UTC
  CLEANUP_CRON="0 5 * * * cd $WS && ${EMBED_ENV}python3 procedures/memory_cleanup.py >> logs/memory_cleanup.log 2>&1"
  upsert_cron "memory_cleanup.py" "dinomem: daily memory deduplication" "$CLEANUP_CRON" "memory_cleanup cron (daily 5:00 UTC)"

  # memory_review — daily at 5:30 UTC (batched, full cycle ~7 days)
  REVIEW_CRON="30 5 * * * cd $WS && ${EMBED_ENV}python3 procedures/memory_review.py >> logs/memory_review.log 2>&1"
  upsert_cron "memory_review.py" "dinomem: daily batched memory review (LLM)" "$REVIEW_CRON" "memory_review cron (daily 5:30 UTC, batched)"

  # cleanup_startup_daily — daily at 2:05 UTC. Prunes bare YYYY-MM-DD.md files
  # (memoryFlush output for startupContext) older than 2 days. Never touches
  # per-item dinomem files, pins, or MEMORY.md.
  STARTUP_CLEANUP_CRON="5 2 * * * cd $WS && python3 procedures/cleanup_startup_daily.py >> logs/cleanup.log 2>&1"
  upsert_cron "cleanup_startup_daily.py" "dinomem: prune bare daily files for startupContext (>2d)" "$STARTUP_CLEANUP_CRON" "cleanup_startup_daily cron (daily 2:05 UTC)"

  # weekly_stats — Sunday 09:00 local, zero LLM, sends stats card to Telegram
  STATS_CRON="0 9 * * 0 python3 $SKILL_DIR/scripts/weekly_stats.py --workspace $WS >> $WS/logs/weekly_stats.log 2>&1"
  upsert_cron "weekly_stats.py" "dinomem: weekly stats card (Sunday 09:00, no LLM)" "$STATS_CRON" "weekly_stats cron (Sunday 09:00)"

  # note_review — daily via OpenClaw cron (LLM judges resolved _note_*.md and deletes them)
  # Registered via OpenClaw cron API, not crontab
  if [ "$DRY_RUN" = 1 ]; then
    plan "register/refresh OpenClaw cron: Daily Note Review (daily 6:00 UTC)"
  else
    python3 - <<PYEOF
import subprocess, json

def _cron_add_argv(job):
    """Build a flag-based `openclaw cron add` argv from a job dict.
    OpenClaw 2026.6.6+ has no `cron add --json <blob>`; jobs are built from flags.
    `--json` here is OUTPUT-only (so we can parse the created job id)."""
    a = ['openclaw', 'cron', 'add']
    name = job.get('name')
    if name:
        a += ['--name', name]
    sched = job.get('schedule', {}) or {}
    if sched.get('kind') == 'cron' and sched.get('expr'):
        a += ['--cron', sched['expr']]
        if sched.get('tz'):
            a += ['--tz', sched['tz']]
    elif sched.get('kind') == 'every' and sched.get('every'):
        a += ['--every', str(sched['every'])]
    elif sched.get('kind') == 'at' and sched.get('at'):
        a += ['--at', str(sched['at'])]
    pay = job.get('payload', {}) or {}
    pkind = pay.get('kind')
    if pkind == 'command':
        argv = pay.get('argv')
        if isinstance(argv, list) and len(argv) >= 3 and argv[0] in ('sh', 'bash') and argv[1] in ('-lc', '-c'):
            a += ['--command', argv[2]]
        elif isinstance(argv, list) and argv:
            import json as _j
            a += ['--command-argv', _j.dumps(argv)]
        elif pay.get('command'):
            a += ['--command', pay['command']]
        if pay.get('cwd'):
            a += ['--command-cwd', pay['cwd']]
        for k, v in (pay.get('env', {}) or {}).items():
            a += ['--command-env', f'{k}={v}']
    else:  # agentTurn (default)
        if pay.get('message'):
            a += ['--message', pay['message']]
        if pay.get('model'):
            a += ['--model', pay['model']]
        if pay.get('thinking'):
            a += ['--thinking', pay['thinking']]
    st = job.get('sessionTarget')
    if st in ('main', 'isolated'):
        a += ['--session', st]
    ts = pay.get('timeoutSeconds')
    if ts:
        a += ['--timeout-seconds', str(ts)]
    dmode = (job.get('delivery', {}) or {}).get('mode')
    if dmode == 'none':
        a += ['--no-deliver']
    elif dmode == 'announce':
        a += ['--announce']
    if job.get('enabled') is False:
        a += ['--disabled']
    a += ['--json']
    return a

def _cron_verify(name):
    """Read back a cron job by name via `cron list --json`. Returns its id, or ''
    if the gateway did not actually store it (silent-failure guard)."""
    try:
        lr = subprocess.run(['openclaw', 'cron', 'list', '--json'], capture_output=True, text=True, timeout=10)
        if lr.returncode != 0:
            return ''
        data = json.loads(lr.stdout)
        joblist = data if isinstance(data, list) else data.get('jobs', {}).get('jobs', data.get('jobs', []))
        for j in (joblist or []):
            if j.get('name','').strip().lower() == name.strip().lower():
                return j.get('id','') or 'exists'
    except Exception:
        return ''
    return ''


def upsert_selfsched(job, label):
    """Upgrade-safe register for a SELF-scheduled agentTurn cron. If a job named
    job['name'] exists, refresh its prompt in place (--message) and keep it enabled
    on its own schedule; else create it. No duplicates, no frozen old prompt."""
    name = job['name']
    existing_id = ''
    try:
        lr = subprocess.run(['openclaw', 'cron', 'list', '--json'], capture_output=True, text=True, timeout=10)
        if lr.returncode == 0:
            data = json.loads(lr.stdout)
            joblist = data if isinstance(data, list) else data.get('jobs', {}).get('jobs', data.get('jobs', []))
            for j in (joblist or []):
                if j.get('name','').strip().lower() == name.strip().lower():
                    existing_id = j.get('id',''); break
    except Exception:
        existing_id = ''
    try:
        if existing_id:
            msg = job.get('payload', {}).get('message', '')
            args = ['openclaw', 'cron', 'edit', existing_id, '--message', msg, '--enable']
            # NOTE: do NOT re-pass --cron/--at/--every on `cron edit`. The OpenClaw
            # cron CLI (2026.6.x) rejects an edit that repeats a schedule flag on an
            # already-scheduled job ('Choose exactly one schedule'). We only refresh
            # message + re-enable here; the existing schedule is preserved as-is.
            subprocess.run(args, capture_output=True, text=True, timeout=15)
            print(f"  \033[32m[ok]\033[0m   {label} OpenClaw cron updated (prompt refreshed, stays enabled)")
        else:
            ar = subprocess.run(_cron_add_argv(job),
                                capture_output=True, text=True, timeout=15)
            if ar.returncode != 0:
                print(f"  \033[33m[warn]\033[0m Could not register {label} cron: {ar.stderr[:120]}")
                print(f"  \033[33m[warn]\033[0m   Add it manually via the OpenClaw cron tool, name='{name}'. Install continues.")
                return
            # (A) READ-BACK: confirm the gateway actually stored it.
            if not _cron_verify(name):
                print(f"  \033[31m[FAIL]\033[0m  {label} cron did NOT persist (read-back by name found nothing). Add it manually via the OpenClaw cron tool, name='{name}'. Install continues.")
                return
            print(f"  \033[32m[ok]\033[0m   {label} OpenClaw cron registered \u2713 verified")
    except Exception as e:
        print(f"  \033[33m[warn]\033[0m {label} upsert failed: {e}")

job = {
    "name": "Daily Note Review",
    "schedule": {"kind": "cron", "expr": "0 6 * * *", "tz": "UTC"},
    "payload": {
        "kind": "agentTurn",
        "message": "Scan all memory/_note_*.md files in $WS/memory/. Resolve each note (today = current UTC date): 1) task_bound notes (have done_when:): verify the done_when condition against workspace state (file exists, feature shipped). If verified, flip status to done and delete the note (promote to _pin_*.md if it has lasting value). Else leave pending. 2) type:project notes (project executor schema, may be added by neuron): these are normally advanced/closed by the neuron Project Advancer (base does not run it), BUT a project can be finished out-of-band by a human-driven session and left status:in_progress, or parked at a safety-gated final step (git push / external action) that the Advancer is forbidden to run — so it would otherwise orphan here. For any type:project note, verify its done_when the SAME way as task_bound (run the locally-checkable condition; e.g. for a git-push done_when run the rev-parse HEAD==@{u} check). If done_when verifies (and/or all steps are [x]), flip status to done and delete/promote it. If it is in_progress and clearly still has unchecked non-gated steps, leave it for the neuron Advancer (if installed). Do not delete a project whose done_when does not verify. 3) stale_after GC: if a note is still pending/in_progress AND done_when was never met AND today > stale_after (default date+30d, or date+7d for reminder/quick-todo notes), delete it as abandoned. 4) Legacy notes with no schema fields: infer the task from content, delete if clearly resolved, else leave. Leave untouched any fields you do not recognize. Report what resolved, what was GC'd, and what remains.",
        "timeoutSeconds": 300
    },
    "sessionTarget": "isolated",
    "delivery": {"mode": "none"}
}
upsert_selfsched(job, "note_review")
PYEOF
  fi

  # TEI @reboot
  # pending_note_reminder — every 3 days via OpenClaw cron (zero LLM pre-filter + LLM evaluate)
  if [ "$DRY_RUN" = 1 ]; then
    plan "register/refresh OpenClaw cron: Pending Note Reminder (every 3 days 9:00 local)"
  else
    python3 - <<PYEOF
import subprocess, json

def _cron_add_argv(job):
    """Build a flag-based `openclaw cron add` argv from a job dict.
    OpenClaw 2026.6.6+ has no `cron add --json <blob>`; jobs are built from flags.
    `--json` here is OUTPUT-only (so we can parse the created job id)."""
    a = ['openclaw', 'cron', 'add']
    name = job.get('name')
    if name:
        a += ['--name', name]
    sched = job.get('schedule', {}) or {}
    if sched.get('kind') == 'cron' and sched.get('expr'):
        a += ['--cron', sched['expr']]
        if sched.get('tz'):
            a += ['--tz', sched['tz']]
    elif sched.get('kind') == 'every' and sched.get('every'):
        a += ['--every', str(sched['every'])]
    elif sched.get('kind') == 'at' and sched.get('at'):
        a += ['--at', str(sched['at'])]
    pay = job.get('payload', {}) or {}
    pkind = pay.get('kind')
    if pkind == 'command':
        argv = pay.get('argv')
        if isinstance(argv, list) and len(argv) >= 3 and argv[0] in ('sh', 'bash') and argv[1] in ('-lc', '-c'):
            a += ['--command', argv[2]]
        elif isinstance(argv, list) and argv:
            import json as _j
            a += ['--command-argv', _j.dumps(argv)]
        elif pay.get('command'):
            a += ['--command', pay['command']]
        if pay.get('cwd'):
            a += ['--command-cwd', pay['cwd']]
        for k, v in (pay.get('env', {}) or {}).items():
            a += ['--command-env', f'{k}={v}']
    else:  # agentTurn (default)
        if pay.get('message'):
            a += ['--message', pay['message']]
        if pay.get('model'):
            a += ['--model', pay['model']]
        if pay.get('thinking'):
            a += ['--thinking', pay['thinking']]
    st = job.get('sessionTarget')
    if st in ('main', 'isolated'):
        a += ['--session', st]
    ts = pay.get('timeoutSeconds')
    if ts:
        a += ['--timeout-seconds', str(ts)]
    dmode = (job.get('delivery', {}) or {}).get('mode')
    if dmode == 'none':
        a += ['--no-deliver']
    elif dmode == 'announce':
        a += ['--announce']
    if job.get('enabled') is False:
        a += ['--disabled']
    a += ['--json']
    return a

def _cron_verify(name):
    """Read back a cron job by name via `cron list --json`. Returns its id, or ''
    if the gateway did not actually store it (silent-failure guard)."""
    try:
        lr = subprocess.run(['openclaw', 'cron', 'list', '--json'], capture_output=True, text=True, timeout=10)
        if lr.returncode != 0:
            return ''
        data = json.loads(lr.stdout)
        joblist = data if isinstance(data, list) else data.get('jobs', {}).get('jobs', data.get('jobs', []))
        for j in (joblist or []):
            if j.get('name','').strip().lower() == name.strip().lower():
                return j.get('id','') or 'exists'
    except Exception:
        return ''
    return ''


def upsert_selfsched(job, label):
    """Upgrade-safe register for a SELF-scheduled agentTurn cron. If a job named
    job['name'] exists, refresh its prompt in place (--message) and keep it enabled
    on its own schedule; else create it. No duplicates, no frozen old prompt."""
    name = job['name']
    existing_id = ''
    try:
        lr = subprocess.run(['openclaw', 'cron', 'list', '--json'], capture_output=True, text=True, timeout=10)
        if lr.returncode == 0:
            data = json.loads(lr.stdout)
            joblist = data if isinstance(data, list) else data.get('jobs', {}).get('jobs', data.get('jobs', []))
            for j in (joblist or []):
                if j.get('name','').strip().lower() == name.strip().lower():
                    existing_id = j.get('id',''); break
    except Exception:
        existing_id = ''
    try:
        if existing_id:
            msg = job.get('payload', {}).get('message', '')
            args = ['openclaw', 'cron', 'edit', existing_id, '--message', msg, '--enable']
            # NOTE: do NOT re-pass --cron/--at/--every on `cron edit`. The OpenClaw
            # cron CLI (2026.6.x) rejects an edit that repeats a schedule flag on an
            # already-scheduled job ('Choose exactly one schedule'). We only refresh
            # message + re-enable here; the existing schedule is preserved as-is.
            subprocess.run(args, capture_output=True, text=True, timeout=15)
            print(f"  \033[32m[ok]\033[0m   {label} OpenClaw cron updated (prompt refreshed, stays enabled)")
        else:
            ar = subprocess.run(_cron_add_argv(job),
                                capture_output=True, text=True, timeout=15)
            if ar.returncode != 0:
                print(f"  \033[33m[warn]\033[0m Could not register {label} cron: {ar.stderr[:120]}")
                print(f"  \033[33m[warn]\033[0m   Add it manually via the OpenClaw cron tool, name='{name}'. Install continues.")
                return
            # (A) READ-BACK: confirm the gateway actually stored it.
            if not _cron_verify(name):
                print(f"  \033[31m[FAIL]\033[0m  {label} cron did NOT persist (read-back by name found nothing). Add it manually via the OpenClaw cron tool, name='{name}'. Install continues.")
                return
            print(f"  \033[32m[ok]\033[0m   {label} OpenClaw cron registered \u2713 verified")
    except Exception as e:
        print(f"  \033[33m[warn]\033[0m {label} upsert failed: {e}")

job = {
    "name": "Pending Note Reminder",
    "schedule": {"kind": "cron", "expr": "0 9 */3 * *"},
    "payload": {
        "kind": "agentTurn",
        "message": "Run: python3 $WS/scripts/check_pending_notes.py\n\nIf exit code is 1 (no output) -> NO_REPLY, stop here, zero LLM cost.\n\nIf exit code is 0 (JSON output) -> for each note in the JSON:\n1. Read the full note file\n2. Evaluate done_when — run any shell command if verifiable, or reason from context\n3. If done -> update status to done in the file, report which ones closed\n4. If not done -> include in reminder summary to user\n\nSend reminder only if there are notes still pending after evaluation. Format: brief list with note title + stale_after date.",
        "timeoutSeconds": 300
    },
    "sessionTarget": "isolated",
    "delivery": {"mode": "announce"}
}
upsert_selfsched(job, "pending_note_reminder")
PYEOF
  fi

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
if [ -f "$OPENCLAW_JSON" ] && [ "$DRY_RUN" != 1 ]; then
  bash "$SKILL_DIR/scripts/file-backup.sh" "$OPENCLAW_JSON" >/dev/null 2>&1 && ok "openclaw.json backed up" || warn "openclaw.json backup failed — continuing"
fi
if [ ! -f "$OPENCLAW_JSON" ]; then
  warn "openclaw.json not found at $OPENCLAW_JSON — skipping config patch"
else
  DINOMEM_DRY_RUN="$DRY_RUN" DINOMEM_OPENCLAW_JSON="$OPENCLAW_JSON" DINOMEM_WS="$WS" python3 - <<'PYEOF'
import json, sys, os, subprocess

path = os.environ["DINOMEM_OPENCLAW_JSON"]
with open(path) as f:
    original = f.read()
cfg = json.loads(original)

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
# memoryFlush ON as the bare-daily-file writer for startupContext, with a guard
# prompt that confines it to memory/YYYY-MM-DD.md and forbids touching MEMORY.md
# (which dinomem owns and regenerates nightly). Bare daily files are pruned by
# cleanup_startup_daily.py so they never accumulate.
MEMORY_FLUSH_PROMPT = (
    "Write any lasting notes ONLY to memory/YYYY-MM-DD.md (today's bare dated file). "
    "Never create, edit, or append MEMORY.md or any other memory/*.md file \u2014 "
    "MEMORY.md is auto-generated by dinomem and will overwrite your edits. "
    "Reply with the exact silent token NO_REPLY if nothing to store."
)
compaction_patch = {
    "mode": "safeguard",
    "truncateAfterCompaction": True,
    "memoryFlush": {
        "enabled": True,
        "softThresholdTokens": 10000,
        "prompt": MEMORY_FLUSH_PROMPT,
    },
}
needs_update = any(compaction.get(k) != v for k, v in compaction_patch.items())
if needs_update:
    compaction.update(compaction_patch)
    changed.append("compaction -> safeguard mode + memoryFlush ON (guarded bare-daily writer for startupContext)")

# contextInjection -> always (root files injected every turn, not skipped on continuation).
# NOTE: the valid OpenClaw config key is `contextInjection`, NOT `workspaceBootstrap`.
# `workspaceBootstrap` is not in the OpenClaw schema; writing it under agents.defaults
# (additionalProperties:false) makes the gateway reject the config and crash on load.
# `always` is already the OpenClaw default; we set it explicitly so intent is documented.
# Also strip any legacy `workspaceBootstrap` left by older installs so the config validates.
if defaults.pop("workspaceBootstrap", None) is not None:
    changed.append("removed legacy invalid key workspaceBootstrap (caused gateway crash)")
if defaults.get("contextInjection") not in (None, "always"):
    defaults["contextInjection"] = "always"
    changed.append("contextInjection -> always (root files injected every turn)")
elif "contextInjection" not in defaults:
    defaults["contextInjection"] = "always"
    changed.append("contextInjection -> always (root files injected every turn)")

# timeoutSeconds floor -> give heavy multi-step / research-then-build turns room to
# finish before the LLM-request idle timeout fires. dinomem-neuron's Project Advancer
# runs long inline steps and spawns sub-agents; on slower providers a single heavy call
# can otherwise trip "LLM request timed out" mid-turn. 300s (5 min) is a deliberate
# middle ground: enough headroom for a heavy step, short enough that a genuinely hung
# request still surfaces without the user waiting forever. NON-CLOBBER: only raises an
# unset or lower value; a user who set a higher ceiling is never lowered. This is a
# base-repo setting (harmless without neuron) — neuron users install base first, no conflict.
TIMEOUT_FLOOR = 300
if not isinstance(defaults.get("timeoutSeconds"), int) or defaults.get("timeoutSeconds", 0) < TIMEOUT_FLOOR:
    prev = defaults.get("timeoutSeconds")
    defaults["timeoutSeconds"] = TIMEOUT_FLOOR
    changed.append(f"agents.defaults.timeoutSeconds -> {TIMEOUT_FLOOR}s floor (was {prev}; heavy-turn headroom)")
# sub-agent runs have their own separate timeout — the Advancer leans on these heavily.
subagents = defaults.setdefault("subagents", {})
if not isinstance(subagents.get("runTimeoutSeconds"), int) or subagents.get("runTimeoutSeconds", 0) < TIMEOUT_FLOOR:
    prev = subagents.get("runTimeoutSeconds")
    subagents["runTimeoutSeconds"] = TIMEOUT_FLOOR
    changed.append(f"agents.defaults.subagents.runTimeoutSeconds -> {TIMEOUT_FLOOR}s floor (was {prev}; sub-agent headroom)")

# bootstrapMaxChars / bootstrapTotalMaxChars -> raise caps to fit what dinomem
# injects, so the policy blocks are never silently truncated. Measured, not a
# fixed delta: read each root bootstrap file's ACTUAL size (the AGENTS.md/TOOLS.md
# blocks have already been appended by this point in the script), and raise the
# caps to max(existing_or_default, measured + buffer). ONLY ever increases — a
# user who manually set a higher cap is never clobbered, and the cap self-corrects
# on every reinstall instead of going stale. Cost note: bigger bootstrap = more
# tokens injected every turn, so we add only a small buffer, not a blanket inflate.
import os, glob as _glob
FILE_DEFAULT = 20000
TOTAL_DEFAULT = 60000
FILE_BUFFER = 10000
TOTAL_BUFFER = 10000
SANITY_FILE = 100000   # warn (not block) if a single file balloons past this
try:
    ws = os.environ["DINOMEM_WS"]
    root_files = ["AGENTS.md", "SOUL.md", "IDENTITY.md", "TOOLS.md", "USER.md", "MEMORY.md"]
    sizes = {}
    for rf in root_files:
        p = os.path.join(ws, rf)
        if os.path.isfile(p):
            sizes[rf] = os.path.getsize(p)
    if sizes:
        max_file = max(sizes.values())
        total = sum(sizes.values())
        biggest = max(sizes, key=sizes.get)
        if max_file > SANITY_FILE:
            print(f"  \033[33m[warn]\033[0m {biggest} is {max_file} chars (>100k) — raising cap anyway, but consider trimming; large bootstrap inflates every prompt.")
        need_file = max_file + FILE_BUFFER
        need_total = total + TOTAL_BUFFER
        cur_file = defaults.get("bootstrapMaxChars", FILE_DEFAULT)
        cur_total = defaults.get("bootstrapTotalMaxChars", TOTAL_DEFAULT)
        new_file = max(cur_file, FILE_DEFAULT, need_file)
        new_total = max(cur_total, TOTAL_DEFAULT, need_total)
        if new_file != defaults.get("bootstrapMaxChars"):
            defaults["bootstrapMaxChars"] = new_file
            changed.append(f"bootstrapMaxChars -> {new_file} (fits largest root file {biggest}={max_file} + {FILE_BUFFER} buffer; raise-only)")
        if new_total != defaults.get("bootstrapTotalMaxChars"):
            defaults["bootstrapTotalMaxChars"] = new_total
            changed.append(f"bootstrapTotalMaxChars -> {new_total} (fits all root files {total} + {TOTAL_BUFFER} buffer; raise-only)")
except Exception as _e:
    print(f"  \033[33m[warn]\033[0m bootstrap cap auto-raise skipped: {_e}")

# thinkingDefault -> medium FLOOR (ensures the agent genuinely internalizes and acts
# on instructions in root files — AGENTS.md, SOUL.md, MEMORY.md, etc. Without
# a minimum thinking floor, injected behavior rules and memory context may be
# acknowledged but not reliably followed).
# TRUE FLOOR, raise-only, and CRITICALLY: only acts on EXPLICIT below-floor values.
# We only lift a thinkingDefault that is explicitly set to off/minimal/low.
# medium/high/xhigh -> already >= floor, untouched. adaptive/max -> >= floor, untouched.
#
# UNSET IS DELIBERATELY LEFT ALONE. 'unset' does NOT mean 'low' — it means the
# provider/model default resolves (OpenClaw thinking.md): Claude 4.6 defaults to
# 'adaptive' (>= our floor), while Opus 4.8/4.7 default 'off'. We cannot know the
# user's model here, so writing 'medium' on unset would CLOBBER a 4.6 user's adaptive
# default DOWN to a fixed medium (the exact bug we are avoiding). Better to respect the
# model's own default than to guess wrong. Users who genuinely set off/minimal/low
# explicitly still get lifted; everyone else keeps their model/provider default.
_THINK_ORDER = {"off": 0, "minimal": 1, "low": 2, "medium": 3, "high": 4, "xhigh": 5}
_cur_think = defaults.get("thinkingDefault")
# Act ONLY on an explicit, known level ranked below medium. Unset (None) -> skip.
# adaptive/max are NOT in _THINK_ORDER by design -> never match, never lowered.
if _cur_think is not None and _cur_think in _THINK_ORDER and _THINK_ORDER[_cur_think] < _THINK_ORDER["medium"]:
    defaults["thinkingDefault"] = "medium"
    changed.append(f"thinkingDefault -> medium floor (was explicit {_cur_think}; below-floor lifted; unset/adaptive/high/max untouched)")

# startupContext ON -> inject last 2 days of bare daily memory on /new and /reset.
# Pairs with the guarded memoryFlush writer above + cleanup_startup_daily.py.
# memory_search pull still handles deep recall; this adds recent raw context on reset.
startup_ctx = defaults.setdefault("startupContext", {})
if startup_ctx.get("enabled") is not True or startup_ctx.get("dailyMemoryDays") != 2:
    startup_ctx["enabled"] = True
    startup_ctx["dailyMemoryDays"] = 2
    changed.append("startupContext.enabled -> true (inject last 2 days of bare daily memory on reset)")

# memorySearch -> TEI openai-compatible (skip if user already has custom provider)
mem_search = defaults.get("memorySearch", {})
existing_provider = mem_search.get("provider")
if existing_provider and existing_provider not in (None, "openai-compatible", "built-in"):
    print(f"  \033[33m[warn]\033[0m memorySearch.provider is '{existing_provider}' (custom) — skipping. dinomem TEI won't be wired automatically. Set manually if needed.")
elif mem_search.get("provider") != "openai-compatible":
    defaults["memorySearch"] = {
        "provider": "openai-compatible",
        "model": "intfloat/multilingual-e5-small",
        "remote": {"baseUrl": "http://localhost:8080/v1"},
        "query": {"hybrid": {"vectorWeight": 0.7, "textWeight": 0.3}},
    }
    changed.append("memorySearch -> TEI openai-compatible (localhost:8080)")

# tools.sessions.visibility -> all (cross-agent sessions_send/sessions_history)
# Default "tree" only covers current session + spawned subagents — blocks cross-agent calls.
# dinomem's memory pipeline needs to reach across agent boundaries.
tools_cfg = cfg.setdefault("tools", {})
sessions_cfg = tools_cfg.setdefault("sessions", {})
if sessions_cfg.get("visibility") != "all":
    sessions_cfg["visibility"] = "all"
    changed.append("tools.sessions.visibility -> all (enables cross-agent sessions_send)")

# tools.deny -> remove sessions_spawn if present
# dinomem-neuron's Project Advancer relies on sessions_spawn to delegate sub-tasks.
# If it's denied, project execution falls back to inline single-turn work and overflows context.
deny_list = tools_cfg.get("deny", [])
if "sessions_spawn" in deny_list:
    tools_cfg["deny"] = [t for t in deny_list if t != "sessions_spawn"]
    changed.append("tools.deny -> removed sessions_spawn (required for project executor sub-tasks)")

# tools.allow -> add sessions_spawn if an explicit allowlist exists and sessions_spawn is missing
# An explicit allow list is a whitelist — omitting sessions_spawn from it blocks the tool
# even if it's not in deny. Only patch if allow is non-empty (empty = no restriction).
allow_list = tools_cfg.get("allow", [])
if allow_list and "sessions_spawn" not in allow_list:
    tools_cfg["allow"] = allow_list + ["sessions_spawn"]
    changed.append("tools.allow -> added sessions_spawn (explicit allowlist was missing it)")

# models.providers -> add tei-embed provider
providers = cfg.setdefault("models", {}).setdefault("providers", {})
if "tei-embed" not in providers:
    providers["tei-embed"] = {
        "api": "openai-completions",
        "baseUrl": "http://localhost:8080/v1",
        "apiKey": "dummy",
        "models": [{"id": "intfloat/multilingual-e5-small", "name": "intfloat/multilingual-e5-small"}],
    }
    changed.append("models.providers.tei-embed -> added")

if changed and os.environ.get("DINOMEM_DRY_RUN") == "1":
    for c in changed:
        print(f"  \033[36m[plan]\033[0m patch openclaw.json: {c}")
    print("  \033[36m[plan]\033[0m would validate against schema, then restart needed")
elif not changed:
    print("  \033[33m[skip]\033[0m openclaw.json already configured")
else:
    # ── Write + validate + SURGICAL recovery ──────────────────────────
    # Write the fully-patched config, then validate against OpenClaw's schema.
    # If it fails, we do NOT blanket-revert to the pre-dinomem backup (that would
    # throw away every dinomem change). Instead we keep MAX dinomem wiring: diff
    # the patched config against the original down to leaf-paths, then re-apply
    # those leaf changes onto the original ONE AT A TIME, validating after each,
    # and DROP only the specific leaf(s) that break validation. Result: a valid
    # config that still carries every dinomem change the running OpenClaw accepts.
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)

    def _run_validate():
        if os.environ.get("DINOMEM_SKIP_CONFIG_VALIDATE") == "1":
            return None
        try:
            return subprocess.run(["openclaw", "config", "validate"],
                                  capture_output=True, text=True, timeout=30)
        except FileNotFoundError:
            return None
        except Exception:
            return None

    validate = _run_validate()

    if validate is not None and validate.returncode != 0:
        # Collect every leaf-path that differs between original and patched.
        orig_cfg = json.loads(original)

        def _leaf_paths(new, old, prefix=()):
            out = []
            if isinstance(new, dict):
                for k, v in new.items():
                    ov = old.get(k) if isinstance(old, dict) else None
                    if isinstance(v, dict) and isinstance(ov, dict):
                        out.extend(_leaf_paths(v, ov, prefix + (k,)))
                    elif ov != v:
                        out.append(prefix + (k,))
                if isinstance(old, dict):
                    for k in old:
                        if k not in new:
                            out.append(prefix + (k,))
            else:
                out.append(prefix)
            return out

        def _get(cfgobj, p):
            cur = cfgobj
            for k in p:
                if not isinstance(cur, dict) or k not in cur:
                    return (False, None)
                cur = cur[k]
            return (True, cur)

        def _set(cfgobj, p, present, val):
            cur = cfgobj
            for k in p[:-1]:
                cur = cur.setdefault(k, {})
            if present:
                cur[p[-1]] = val
            else:
                cur.pop(p[-1], None)  # dinomem removed this key

        paths = _leaf_paths(cfg, orig_cfg)
        # Start from the ORIGINAL, greedily add each dinomem leaf that still validates.
        recovered = json.loads(original)
        kept, dropped = [], []
        for p in paths:
            present, val = _get(cfg, p)
            before_present, before_val = _get(recovered, p)
            _set(recovered, p, present, val)
            with open(path, "w") as f:
                json.dump(recovered, f, indent=2)
            v = _run_validate()
            if v is not None and v.returncode != 0:
                # this leaf breaks the config -> revert just this leaf, keep the rest
                _set(recovered, p, before_present, before_val)
                dropped.append(".".join(p))
            else:
                kept.append(".".join(p))
        # Final write of the max-valid recovered config.
        with open(path, "w") as f:
            json.dump(recovered, f, indent=2)
        final = _run_validate()
        if final is not None and final.returncode != 0:
            # Even the original leaves fail (pre-existing invalid config, not us).
            # Restore exact original bytes so we never leave it worse than we found it.
            with open(path, "w") as f:
                f.write(original)
            detail = (final.stderr or final.stdout or "").strip()
            print("  \033[31m[fail]\033[0m openclaw.json was already invalid before dinomem "
                  "(recovery could not produce a valid config) — restored your original bytes:")
            for line in detail.splitlines():
                line = line.strip()
                if line:
                    print(f"           {line}")
            sys.exit(3)
        # We produced a valid config that keeps the accepted dinomem changes.
        print("  \033[33m[warn]\033[0m Some config changes were rejected by your OpenClaw "
              "version's schema and were skipped (kept everything else):")
        for d in dropped:
            print(f"           \033[33mskipped\033[0m {d}")
        detail = (validate.stderr or validate.stdout or "").strip()
        for line in detail.splitlines():
            line = line.strip()
            if line:
                print(f"           {line}")
        print(f"  \033[32m[ok]\033[0m   openclaw.json validated after recovery "
              f"({len(kept)} change(s) kept, {len(dropped)} skipped).")
    else:
        for c in changed:
            print(f"  \033[32m[ok]\033[0m   patched: {c}")
        if validate is not None:
            print("  \033[32m[ok]\033[0m   openclaw.json validated against schema")
    print("  \033[33m[warn]\033[0m Restart OpenClaw: openclaw gateway restart")
PYEOF
fi

# ── 5b) smart-cache-pro (compression-only) plugin ────────────────────────────
# Bundle the token-discipline plugin: it compresses verbose tool output before it
# enters context (tee'd full output to disk, nothing lost). Cloned next to the
# workspace and wired into openclaw.json via plugins.load.paths + plugins.entries.
# Force-installed: re-clone/pull + overwrite the entry every run (idempotent).
# Skip with --no-smart-cache. Self-cleaning on disk; does not touch OpenClaw memory DB.
if [ "$DO_SMART_CACHE" = 1 ]; then
  hr "smart-cache-pro plugin (compression-only)"
  SC_DIR="$OPENCLAW_DIR/smart-cache-pro"
  if ! command -v git >/dev/null 2>&1; then
    warn "git not found — skipping smart-cache-pro (install git or re-run with --no-smart-cache)"
  elif [ "$DRY_RUN" = 1 ]; then
    if [ -d "$SC_DIR/.git" ]; then
      plan "git -C $SC_DIR fetch + reset to origin/$SMART_CACHE_BRANCH (force-refresh)"
    else
      plan "git clone -b $SMART_CACHE_BRANCH $SMART_CACHE_REPO -> $SC_DIR"
    fi
    plan "wire plugins.load.paths += $SC_DIR and plugins.entries['smart-cache-pro'].enabled=true in $OPENCLAW_JSON"
  else
    # Clone (or force-refresh) the pinned branch.
    if [ -d "$SC_DIR/.git" ]; then
      if git -C "$SC_DIR" fetch --depth 1 origin "$SMART_CACHE_BRANCH" >/dev/null 2>&1 \
         && git -C "$SC_DIR" reset --hard "origin/$SMART_CACHE_BRANCH" >/dev/null 2>&1; then
        ok "smart-cache-pro refreshed to origin/$SMART_CACHE_BRANCH"
      else
        warn "could not refresh $SC_DIR — using existing checkout"
      fi
    elif [ -e "$SC_DIR" ]; then
      warn "$SC_DIR exists but is not a git repo — using as-is"
    else
      if git clone --depth 1 -b "$SMART_CACHE_BRANCH" "$SMART_CACHE_REPO" "$SC_DIR" >/dev/null 2>&1; then
        ok "cloned smart-cache-pro ($SMART_CACHE_BRANCH) -> $SC_DIR"
      else
        warn "git clone failed ($SMART_CACHE_REPO @ $SMART_CACHE_BRANCH) — skipping plugin wiring"
        SC_DIR=""
      fi
    fi
    # Wire into openclaw.json (add-if-absent load.paths + enabled entry). Idempotent.
    if [ -n "$SC_DIR" ] && [ -f "$OPENCLAW_JSON" ]; then
      python3 - "$OPENCLAW_JSON" "$SC_DIR" <<'PYEOF'
import json, sys
path, sc_dir = sys.argv[1], sys.argv[2]
with open(path) as f:
    cfg = json.load(f)
plugins = cfg.setdefault("plugins", {})
load = plugins.setdefault("load", {})
paths = load.get("paths")
if not isinstance(paths, list):
    paths = []
changed = []
if sc_dir not in paths:
    paths.append(sc_dir); changed.append("plugins.load.paths += smart-cache-pro")
load["paths"] = sorted(set(paths))            # de-dupe defensively
entries = plugins.setdefault("entries", {})
ent = entries.get("smart-cache-pro")
if not isinstance(ent, dict):
    entries["smart-cache-pro"] = {"enabled": True}; changed.append("plugins.entries['smart-cache-pro'] created")
elif ent.get("enabled") is not True:
    ent["enabled"] = True; changed.append("plugins.entries['smart-cache-pro'].enabled=true")
# If an allowlist is in use, add the id (membership-based, harmless if absent).
allow = plugins.get("allow")
if isinstance(allow, list) and "smart-cache-pro" not in allow:
    allow.append("smart-cache-pro"); changed.append("plugins.allow += smart-cache-pro")
# bundledDiscovery is a REQUIRED companion to plugins.allow on OpenClaw 2026.6.x+.
# An allow list WITHOUT bundledDiscovery makes the new schema reject the whole
# config ("plugins: Invalid input") -> gateway won't start -> total bot silence.
# "compat" preserves legacy bundled provider/channel discovery (won't kill chat
# channels). Only stamp it when an allow list is actually present.
if isinstance(allow, list) and plugins.get("bundledDiscovery") not in ("compat", "allowlist"):
    plugins["bundledDiscovery"] = "compat"; changed.append('plugins.bundledDiscovery -> "compat"')
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
if changed:
    for c in changed: print(f"  \033[32m[ok]\033[0m   {c}")
else:
    print("  \033[33m[skip]\033[0m smart-cache-pro already wired in openclaw.json")
PYEOF
      warn "Restart OpenClaw to load smart-cache-pro: openclaw gateway restart"
    elif [ -n "$SC_DIR" ]; then
      warn "openclaw.json not found at $OPENCLAW_JSON — clone done but plugin not wired"
    fi
  fi
fi

# ── 6) Wire AGENTS.md ──────────────────────────────────────────────
hr "AGENTS.md"
AGENTS="$WS/AGENTS.md"
if [ -f "$AGENTS" ] && [ "$DRY_RUN" != 1 ]; then
  bash "$SKILL_DIR/scripts/file-backup.sh" "$AGENTS" >/dev/null 2>&1 && ok "AGENTS.md backed up" || warn "AGENTS.md backup failed — continuing"
fi
BEGIN="<!-- BEGIN:dinomem (managed — do not edit between markers) -->"
END="<!-- END:dinomem -->"
# Body is literal (quoted heredoc): inner double-quotes and <angle-bracket>
# placeholders must NOT be shell-evaluated. Markers added around it after.
DINOMEM_BODY=$(cat <<'DINOMEM_AGENTS_BODY'
## dinomem
  memory_index: {file: MEMORY.md, instruction: topic in MEMORY.md → memory_search then memory_get}
  open_work: open _note_ files (status in_progress|pending) are auto-injected each session by the dinomem-open-notes hook as a must-read manifest — read the relevant one and resume from its resume_state before answering; do NOT restart finished work.
  constraints:
    M0: context_unclear → memory_search + memory_get; fallback: ask
    M1:
      before: tool/script with side effects OR message naming an entity/repo/feature matching an open _note_
      action:
        - memory_search first
        - read open notes (see open_work manifest) before building
      enforce: mandatory; fires on entity-name match too, not only literal "build" requests
    M2:
      when: named entity | temporal ref | implicit ref | continuation request
      action: rewrite implicit query → memory_search FIRST (before fs/exec/any tool)
      enforce: no exceptions; memory before filesystem; violating M2 = repeating mistakes
    M3_query_style:
      applies_to: memory_search
      prefer: natural_language
      avoid: technical_identifiers | code_terms | exact_strings | variable_names
      enforce: rewrite query to natural language before calling any memory tool

  investigate_before_act:
    triggers: bug_report | fix_request | refactor | cross_entity_claim | any assertion about file/git/version/config
    rule:
      inspect: read real artifact / run command before answering; memory is not a source
      reproduce: get failure locally before fixing
      verify: re-run after fix; assert symptom gone
      cross_entity: shared lineage != shared bug; check target directly
      stale_claim: prior "it's fixed" is not evidence; re-verify
    enforce: mutable facts need live check every turn

  memory_tools:
    memory_search: simple recall — facts, preferences, decisions, context; default for most queries
  memory_recall:
    after_search: memory_get on relevant result
    skip: do not call memory_search every turn

  skills:
    memory_pin: when user says remember/pin/note this, you commit to deferred work, or a todo/reminder/time-bound/project task arises → read skill "memory-pinning" for _pin_/_note_/project format + done_when rules
    self_config: when user implies changing behavior/rules/workflow/persona/tools/preferences → read skill "self-config"
    backup_restore: when user asks to undo/restore a file or memory change, or what backups exist → read skill "backup-restore"
DINOMEM_AGENTS_BODY
)
BLOCK="$BEGIN
$DINOMEM_BODY
$END"

[ "$DRY_RUN" = 1 ] || touch "$AGENTS"
if grep -qF "$BEGIN" "$AGENTS" 2>/dev/null; then
  # Block already present. Refresh it in place ONLY under --force, so upgrades
  # from an older dinomem (longer block, no hook/skills stubs) actually pick up
  # the current block instead of keeping stale text. Strip the old
  # BEGIN..END span first, then append the fresh block (idempotent upsert —
  # never stacks a second block).
  if [ "$FORCE" = 1 ]; then
    if [ "$DRY_RUN" = 1 ]; then
      plan "refresh dinomem managed block in AGENTS.md (strip old BEGIN..END, write current)"
    else
      _tmp_agents="$(mktemp)"
      # Delete the inclusive BEGIN..END region (fixed-string match), keep everything else.
      awk -v b="$BEGIN" -v e="$END" '
        index($0,b){skip=1}
        !skip{print}
        index($0,e){skip=0}
      ' "$AGENTS" > "$_tmp_agents"
      # Trim trailing blank lines the removal may leave, then append fresh block.
      awk 'NF{last=NR} {lines[NR]=$0} END{for(i=1;i<=last;i++) print lines[i]}' "$_tmp_agents" > "$AGENTS"
      rm -f "$_tmp_agents"
      printf '\n%s\n' "$BLOCK" >> "$AGENTS"
      ok "AGENTS.md block refreshed (old block stripped, current block written)"
    fi
  else
    skip "AGENTS.md already wired (re-run with --force to refresh the managed block)"
  fi
elif [ "$DRY_RUN" = 1 ]; then
  plan "append dinomem managed block to AGENTS.md"
else
  printf '\n%s\n' "$BLOCK" >> "$AGENTS"
  ok "AGENTS.md wired"
fi

# ── 6b) Wire TOOLS.md ────────────────────────────────────────────────────────
hr "TOOLS.md"
TOOLS="$WS/TOOLS.md"
TOOLS_MARKER="# dinomem: workspace_backup"
TOOLS_BODY=$(cat <<'DINOMEM_TOOLS_BODY'
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
      mode: read_write

  config_tool:
    path: tools/config_tool.py
    type: exec
    capabilities:
      - safe_write_agent_root_config
      - append_section
      - overwrite_file
      - patch_section_by_key
      - remove_section_by_key
    when_to_use: safe writer for agent root config files; routing map in config_tool.py docstring
    subcommands:
      append:
        usage: "config_tool.py append <file> <content>"
        inputs:
          file:    { type: string, required: true, note: "Target root config filename." }
          content: { type: string, required: true, note: "Text appended to the file." }
      write:
        usage: "config_tool.py write <file> <content>"
        inputs:
          file:    { type: string, required: true, note: "Target root config filename." }
          content: { type: string, required: true, note: "Full replacement content." }
      patch:
        usage: "config_tool.py patch <file> <section_key> <content>"
        inputs:
          file:        { type: string, required: true, note: "Target root config filename." }
          section_key: { type: string, required: true, note: "Section heading/key to replace." }
          content:     { type: string, required: true, note: "New section body." }
      remove:
        usage: "config_tool.py remove <file> <section_key>"
        inputs:
          file:        { type: string, required: true, note: "Target root config filename." }
          section_key: { type: string, required: true, note: "Section heading/key to remove." }
    output:
      type: json
      note: "Each command prints a JSON result of the write operation."
    constraints:
      mode: read_write
      confirm_before_write: [SOUL.md, IDENTITY.md, AGENTS.md]
      skip_confirm: [TOOLS.md, USER.md]
DINOMEM_TOOLS_BODY
)
TOOLS_BLOCK="$TOOLS_MARKER
$TOOLS_BODY"

if grep -qF "$TOOLS_MARKER" "$TOOLS" 2>/dev/null; then
  skip "TOOLS.md already has workspace_backup entry"
elif [ "$DRY_RUN" = 1 ]; then
  plan "append dinomem tool entries to TOOLS.md"
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
if [ "$DRY_RUN" = 1 ]; then
  hr "dry run complete"
  echo "  Preview only — nothing was written (no files, crons, Docker, or config)."
  echo "  [plan] lines above show what a real run would do."
  echo "  Re-run without --dry-run to apply."
  echo "  Undo (after a real install): bash $SKILL_DIR/scripts/uninstall.sh --workspace $WS --agent-id $AGENT_ID"
  exit 0
fi

# ── Hook liveness self-check (L3/L3b) ───────────────────────────────────────
# `openclaw hooks enable` only sets the config flag; it does NOT guarantee the
# gateway can actually LOAD the hook. If $WS is not the gateway's scanned
# workspace, the hook lands in an unscanned $WS/hooks/ and silently never fires.
# Assert eligibility via `hooks check --json` (grep on the human table is
# unreliable: emoji wrap splits names). On failure, fall back to installing the
# hook pack into the always-scanned global ~/.openclaw/hooks/<name>/ and re-enable.
if command -v openclaw >/dev/null 2>&1 && openclaw status >/dev/null 2>&1; then
  hr "Hook liveness self-check"
  GLOBAL_HOOKS_DIR="${OPENCLAW_DIR:-$HOME/.openclaw}/hooks"
  for _hk in dinomem-reset-extract dinomem-open-notes; do
    _elig="$(openclaw hooks check --json 2>/dev/null | python3 -c "import json,sys;
try:
  d=json.load(sys.stdin); print('yes' if '$_hk' in d.get('hooks',{}).get('eligible',[]) else 'no')
except Exception: print('unknown')" 2>/dev/null)"
    if [ "$_elig" = "yes" ]; then
      ok "$_hk is eligible (gateway can load it)"
    else
      warn "$_hk NOT eligible from \$WS/hooks/ — installing into global $GLOBAL_HOOKS_DIR/ as fallback"
      _hksrc="$SKILL_DIR/hooks/$_hk"
      if [ -d "$_hksrc" ]; then
        mkdir -p "$GLOBAL_HOOKS_DIR"
        rm -rf "$GLOBAL_HOOKS_DIR/$_hk"
        cp -r "$_hksrc" "$GLOBAL_HOOKS_DIR/$_hk"
        openclaw hooks enable "$_hk" >/dev/null 2>&1 || true
        warn "$_hk copied to global hooks dir — RESTART OpenClaw, then it will load (verify: openclaw hooks check --json)"
      else
        warn "$_hk source not found at $_hksrc — cannot self-heal; check the install"
      fi
    fi
  done
fi

hr "done"
echo "  dinomem installed for agent: $AGENT_ID"
echo "  workspace: $WS"
echo ""
echo "  Next steps:"
echo "  1. Restart OpenClaw:  openclaw gateway restart"
echo "  2. Verify TEI:        bash $SKILL_DIR/scripts/doctor.sh"
echo "                        (or: curl http://localhost:8080/health)"
echo "  3. Add to tools.allow in openclaw.json: memory_search, memory_get"
echo "  4. First extraction:  python3 $WS/procedures/auto_session_reset.py"
echo ""
echo ""
echo "  ⚠️  ACTION REQUIRED: Compaction tuning"
echo "  Skipping this hurts memory quality and response performance."
echo "  Takes 2 minutes — do this before restarting OpenClaw."
echo "  Guide: https://github.com/02-dino/dinomem#compaction-tuning-manual-strongly-recommended"
echo ""
echo "  MODEL_HINT: memory scripts use your OpenClaw default model."
echo "  Base dinomem is all no-reasoning bulk (extract/review) — they honor"
echo "  \$DINOMEM_CHEAP_MODEL if set. Unset = no change (default-safe)."
echo "  To cut cost, set DINOMEM_CHEAP_MODEL=<cheapest high-context model you have>."
echo "  (Reasoning scripts exist only if you add dinomem-neuron; they keep the default.)"
echo "  SAME-TIER: OpenClaw compaction is also no-reasoning bulk — set"
echo "  agents.defaults.compaction.model to the SAME cheap high-context model."
echo "  AGENT_ACTION: if installing autonomously, read the user's openclaw.json model"
echo "  catalog, pick the cheapest model with enough context (~80k+), and set BOTH"
echo "  DINOMEM_CHEAP_MODEL and compaction.model to it (or recommend it to your human)."
echo "  Keep model.primary on the default (neuron's reasoning scripts, if installed,"
echo "  stay there too). Never auto-pick a low-context model. Unsure = leave unset."
echo "  (If you later add dinomem-neuron, its installer re-surfaces this with the"
echo "  reasoning tier included — you don't need to remember it now.)"
echo "  Guide: https://github.com/02-dino/dinomem#model-selection"
echo ""
echo "  ✨ Want your agent to learn, not just remember?"
echo "  https://github.com/02-dino/dinomem#want-more--dinomem-neuron-private-repo"
echo ""
echo "  License: MIT — https://github.com/02-dino/dinomem/blob/main/LICENSE"
echo "  Undo: bash $SKILL_DIR/scripts/uninstall.sh --workspace $WS --agent-id $AGENT_ID"
