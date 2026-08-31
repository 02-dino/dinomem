#!/usr/bin/env bash
# dinomem — install script
# Sets up Dino Agent Memory System for an OpenClaw agent.
# Idempotent: safe to run multiple times.
#
# Usage:
#   bash scripts/install.sh [--workspace DIR] [--agent-id ID] [--no-docker] [--no-cron] [--no-backup-cron] [--no-smart-cache] [--git-snapshot] [--force] [--dry-run]
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
#   --no-git-snapshot Disable the git snapshot safety net (default: ON).
#                     When on, an ISOLATED snapshot store (.dinomem-snap.git)
#                     commits all non-ignored changes every 15 min
#                     (disk-aware cleanup, lfs media handling, history retention).
#                     It NEVER touches your own repo: separate git-dir, private
#                     info/exclude, no .gitignore dropped in your tree.
#                     (--git-snapshot still accepted; it's the default now.)
#                     See features/git-autosnapshot/README.md.
#   --force           Overwrite existing files
#   --dry-run         Preview every change without writing anything (no files,
#                     no crons, no Docker, no config patch). Idempotency-aware:
#                     reports would-create vs already-present.
set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Shared instance-discovery lib (single-agent / multi-agent / multi-gateway aware).
# Fallback-safe: absent or non-systemd host => selection returns "default" mode and
# the installer keeps its existing single-config behavior. (Same lib neuron sources.)
if [ -f "$SKILL_DIR/lib/discover_instances.sh" ]; then
  # shellcheck source=/dev/null
  . "$SKILL_DIR/lib/discover_instances.sh"
fi
WS="${OPENCLAW_WORKSPACE:-$HOME/.openclaw/workspace}"
AGENT_ID=""
INSTANCE_ID=""   # --instance <agent-id>: scripted single-instance target (skips prompt)
DO_DOCKER=1
DO_CRON=1
DO_BACKUP_CRON=1
DO_SMART_CACHE=1
DO_GIT_SNAPSHOT=1   # default-ON: ISOLATED git snapshot store (.dinomem-snap.git — never touches your own repo). Opt out with --no-git-snapshot. See features/git-autosnapshot
DO_GREP_GUARD=1     # default-ON (ANNOUNCED at install): PATH-ahead shim blocking ONLY broad recursive greps over large trees. Opt out with --no-grep-guard. See features/grep-guard
FORCE=0
DRY_RUN=0
# --repair-cron: idempotent "just fix the crons" mode. Skips the heavy/one-time
# phases (docker/TEI, pip, file copy, config/hook wiring, git-snapshot, smart-cache)
# and flows straight to cron registration + gate + self-check. Safe to re-run any
# time a fresh install left the Daily Note Review / Pending Note Reminder / Note
# Cron Gate lanes unregistered.
REPAIR_CRON=0

# smart-cache-pro (compression-only) — bundled token-discipline plugin. Overridable.
SMART_CACHE_REPO="${SMART_CACHE_REPO:-https://github.com/02-dino/smart-cache-pro}"
SMART_CACHE_BRANCH="${SMART_CACHE_BRANCH:-feat/compression-only-generalized}"

while [ $# -gt 0 ]; do
  case "$1" in
    --workspace)  WS="$2"; shift 2 ;;
    --agent-id)   AGENT_ID="$2"; shift 2 ;;
    --instance)   INSTANCE_ID="$2"; shift 2 ;;
    --no-docker)  DO_DOCKER=0; shift ;;
    --no-cron)         DO_CRON=0; shift ;;
    --repair-cron)     REPAIR_CRON=1; DO_CRON=1; DO_DOCKER=0; DO_SMART_CACHE=0; DO_GIT_SNAPSHOT=0; shift ;;
    --no-backup-cron)  DO_BACKUP_CRON=0; shift ;;
    --no-smart-cache)  DO_SMART_CACHE=0; shift ;;
    --git-snapshot)    DO_GIT_SNAPSHOT=1; shift ;;
    --no-git-snapshot) DO_GIT_SNAPSHOT=0; shift ;;
    --grep-guard)      DO_GREP_GUARD=1; shift ;;
    --no-grep-guard)   DO_GREP_GUARD=0; shift ;;
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
# wire_managed_block FILE BEGIN END BLOCK LABEL — idempotent UNCONDITIONAL upsert of
# one managed marker span. WHY: the old skip-unless-force wiring meant an upgrade
# SILENTLY kept a stale managed block (e.g. missing newer hook/skill stubs) forever.
# Contract: if the BEGIN..END span is present, strip EXACTLY that span (fixed-string
# match on THESE markers only — a different block's markers are untouched) then
# append the fresh BLOCK; if absent, just append. ALWAYS writes the current block
# regardless of same/different (identical content = same result → re-runs stay
# idempotent; no --force needed). Honors DRY_RUN via plan(). Gotcha: two awk passes
# — (1) strip inclusive span, (2) trim trailing blanks the strip leaves — so blank
# lines never accumulate across re-runs.
wire_managed_block() {
  local _file="$1" _begin="$2" _end="$3" _block="$4" _label="$5"
  if [ "$DRY_RUN" = 1 ]; then
    if grep -qF "$_begin" "$_file" 2>/dev/null; then
      plan "refresh managed block in $_label (strip old BEGIN..END, write current)"
    else
      plan "wire managed block into $_label"
    fi
    return 0
  fi
  touch "$_file"
  if grep -qF "$_begin" "$_file" 2>/dev/null; then
    local _tmp; _tmp="$(mktemp)"
    awk -v b="$_begin" -v e="$_end" '
      index($0,b){skip=1}
      !skip{print}
      index($0,e){skip=0}
    ' "$_file" > "$_tmp"
    awk 'NF{last=NR} {lines[NR]=$0} END{for(i=1;i<=last;i++) print lines[i]}' "$_tmp" > "$_file"
    rm -f "$_tmp"
    printf '\n%s\n' "$_block" >> "$_file"
    ok "$_label block refreshed (old block stripped, current block written)"
  else
    printf '\n%s\n' "$_block" >> "$_file"
    ok "$_label wired"
  fi
}
# openclaw_running: guarded 'is the gateway up?' probe. `openclaw status` can BLOCK
# indefinitely when the gateway socket/lock is contended, and under `set -e` a bare
# call in a preflight `if` FREEZES the whole installer (silent kill by the outer
# session timeout -> 'base installer exited nonzero' with no error). Wrap it in a
# hard timeout so a hung/absent gateway degrades to 'not running' instead of hanging.
# One helper, called everywhere (DRY), so this guarantee holds at all sites.
openclaw_running() {
  command -v openclaw >/dev/null 2>&1 || return 1
  # Probe the TARGET instance, not the default: when a multi-instance selection set
  # OPENCLAW_STATE_DIR, honor it so we don't false-report "not running" against the
  # wrong gateway. Empty -> default instance (back-compat, single-agent hosts).
  if [ -n "${OPENCLAW_STATE_DIR:-}" ]; then
    OPENCLAW_STATE_DIR="$OPENCLAW_STATE_DIR" timeout 10 openclaw status >/dev/null 2>&1
  else
    timeout 10 openclaw status >/dev/null 2>&1
  fi
}

# resolve_memory_db <agent_id> <openclaw_dir>: print the REAL sqlite DB path for
# this box. WHY: OpenClaw's DB layout is version-dependent
# (agents/<id>/agent/openclaw-agent.sqlite on modern, memory/<id>.sqlite on
# legacy) and the internal agent-id is not always the workspace name, so any
# hardcoded convention is a guess that crashes some installer with "unable to
# open database file". The single source of truth is OpenClaw itself:
# `openclaw memory status` prints `Store: <path>`. Ask it here (install time)
# and bake the answer; the runtime call is ~25s, far too slow for the daily
# cron. Order: env override > openclaw Store: > legacy convention probe (only if
# the CLI is absent/errors). Never emits a foreign /home/* or bare hardcode.
resolve_memory_db() {
  local _agent="$1" _ocdir="$2" _store=""
  if [ -n "${DINOMEM_MEMORY_DB:-}" ]; then printf '%s\n' "$DINOMEM_MEMORY_DB"; return 0; fi
  if command -v openclaw >/dev/null 2>&1; then
    _store="$(timeout 40 openclaw memory status 2>/dev/null \
      | awk -F': *' '/^Store:/{print $2; exit}')"
    _store="${_store/#\~/$HOME}"
    if [ -n "$_store" ]; then printf '%s\n' "$_store"; return 0; fi
  fi
  local _cand
  for _cand in \
    "$_ocdir/agents/$_agent/agent/openclaw-agent.sqlite" \
    "$_ocdir/memory/$_agent.sqlite" \
    "$_ocdir/memory/main.sqlite"; do
    [ -e "$_cand" ] && { printf '%s\n' "$_cand"; return 0; }
  done
  printf '%s\n' "$_ocdir/agents/$_agent/agent/openclaw-agent.sqlite"
}
# SUDO: surgical auto-elevation for the FEW steps that genuinely need root (system
# package installs: python/docker). Empty when already root or when sudo is absent,
# so it degrades to a bare call (which then warns cleanly instead of silently
# failing). NEVER used for workspace files / cron / config — those MUST stay owned
# by the invoking user, so running the whole installer as root is WRONG (root-owned
# files break the user's gateway). This is the noob-proof middle ground: elevate the
# 2 package steps automatically, keep everything else as the user.
SUDO=""; [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1 && SUDO="sudo"
# plan: in --dry-run, print what WOULD happen instead of doing it.
plan() { printf '  \033[36m[plan]\033[0m %s\n' "$*"; }

# subst FILE PLACEHOLDER VALUE  — portable in-place placeholder replacement.
# WHY not `sed -i`: BSD sed (macOS) requires an explicit backup suffix
# (`sed -i ''`) while GNU sed (Linux) forbids it, so a single `sed -i "s|..|..|"`
# crashes on one of the two platforms. AND the replacement VALUE is a filesystem
# path that can legally contain `|`, `/`, `&`, or other sed-special chars, which
# breaks any fixed `s<delim>...<delim>` expression and mangles `&`/backrefs.
# This helper sidesteps BOTH: no `-i` (write to a temp file, then move), and a
# literal (non-regex) replace via awk index/substr so the value needs no
# escaping and no delimiter can collide. Portable across BSD/GNU userland.
# _SUBST_SEQ: monotonic counter so each subst() call gets a UNIQUE temp name.
# `$_f.tmp.$$` alone collides when subst is called multiple times on the SAME
# file within one process (e.g. _same_content substitutes 3 placeholders on one
# mktemp file) -> awk 'cannot open file' race. $$.$seq is unique per call.
subst() {
  _f="$1"; _ph="$2"; _val="$3"
  # Unique temp per call via mktemp (in the SAME dir so the final mv is atomic,
  # not a cross-filesystem copy). `$_f.tmp.$$` alone collided when subst ran
  # multiple times on one file within a process (_same_content substitutes 3
  # placeholders on one mktemp file) -> awk 'cannot open file' race.
  _tmp="$(mktemp "${_f}.tmp.XXXXXX")" || return 1
  PH="$_ph" VAL="$_val" awk '
    BEGIN { ph=ENVIRON["PH"]; val=ENVIRON["VAL"]; L=length(ph) }
    {
      out=""; s=$0
      while ((p=index(s,ph))>0) {
        out = out substr(s,1,p-1) val
        s = substr(s,p+L)
      }
      print out s
    }
  ' "$_f" > "$_tmp" && mv "$_tmp" "$_f"
}
# _same_content SRC DST — true (0) if the INSTALLED file already equals what we'd
# write (source with placeholders substituted). Lets upgrades be content-aware:
# copy only when the shipped file actually CHANGED, skip when identical. Avoids
# both the old blind skip-if-exists (upgrades never landed) and a blind --force
# clobber (would wipe user edits every run). Compares the SUBSTITUTED source vs
# the installed file so placeholder differences don't cause false "changed".
_same_content() {
  local _src="$1" _dst="$2"
  [ -f "$_dst" ] || return 1
  local _tmp; _tmp="$(mktemp)" || return 1
  # Substitute ALL THREE placeholders in ONE awk pass (not 3 subst() calls on the
  # same temp) so there is no repeated temp-file churn to race the git-autosnapshot
  # cron on /tmp (which produced cosmetic 'awk: cannot open file' noise). Literal
  # index/substr replace, no regex, order-independent.
  W="$WS" S="$SESSIONS_DIR" A="$AGENT_ID" D="$MEMORY_DB" awk '
    BEGIN {
      n=4
      ph[1]="DINOMEM_WORKSPACE_PLACEHOLDER";        val[1]=ENVIRON["W"]
      ph[2]="DINOMEM_AGENT_SESSIONS_PLACEHOLDER"; val[2]=ENVIRON["S"]
      ph[3]="DINOMEM_AGENT_ID_PLACEHOLDER";           val[3]=ENVIRON["A"]
      ph[4]="DINOMEM_DB_PLACEHOLDER";                 val[4]=ENVIRON["D"]
    }
    {
      s=$0
      for (i=1;i<=n;i++) {
        L=length(ph[i]); out=""
        while ((p=index(s,ph[i]))>0) { out=out substr(s,1,p-1) val[i]; s=substr(s,p+L) }
        s=out s
      }
      print s
    }
  ' "$_src" > "$_tmp" 2>/dev/null
  if cmp -s "$_tmp" "$_dst"; then rm -f "$_tmp"; return 0; else rm -f "$_tmp"; return 1; fi
}
# copy_engine_file REL_PATH — the ONE copy primitive (DRY). Copies $SKILL_DIR/REL
# -> $WS/REL with placeholder substitution, and decides copy-vs-skip by CONTENT,
# not mere existence, so re-running the installer actually UPGRADES changed engine
# files (the whole point) while leaving unchanged ones untouched. --force still
# forces. Handles dry-run + mkdir parent. This replaces the old per-file
# skip-if-exists blocks AND is what the auto-discovery loops call, so a NEW engine
# file ships automatically without editing a hardcoded manifest.
copy_engine_file() {
  local _rel="$1" _src _dst
  _src="$SKILL_DIR/$_rel"; _dst="$WS/$_rel"
  [ -f "$_src" ] || { warn "source missing: $_rel (skipped)"; return 0; }
  if [ "$FORCE" = 0 ] && _same_content "$_src" "$_dst"; then
    skip "$_rel (up-to-date)"
    return 0
  fi
  if [ "$DRY_RUN" = 1 ]; then
    if [ -f "$_dst" ]; then plan "UPGRADE $_rel (content changed)"; else plan "install $_rel"; fi
    return 0
  fi
  mkdir -p "$(dirname "$_dst")"
  # Copy + substitute ALL THREE placeholders in ONE awk pass (not 3 subst() calls)
  # to avoid repeated temp-file churn racing the git-autosnapshot cron on /tmp
  # (which produced cosmetic 'awk: cannot open file' noise on re-runs). Atomic:
  # write to a same-dir temp, then mv into place.
  local _ctmp; _ctmp="$(mktemp "${_dst}.tmp.XXXXXX")" || { warn "mktemp failed for $_rel"; return 1; }
  W="$WS" S="$SESSIONS_DIR" A="$AGENT_ID" D="$MEMORY_DB" awk '
    BEGIN {
      n=4
      ph[1]="DINOMEM_WORKSPACE_PLACEHOLDER";        val[1]=ENVIRON["W"]
      ph[2]="DINOMEM_AGENT_SESSIONS_PLACEHOLDER"; val[2]=ENVIRON["S"]
      ph[3]="DINOMEM_AGENT_ID_PLACEHOLDER";           val[3]=ENVIRON["A"]
      ph[4]="DINOMEM_DB_PLACEHOLDER";                 val[4]=ENVIRON["D"]
    }
    {
      s=$0
      for (i=1;i<=n;i++) {
        L=length(ph[i]); out=""
        while ((p=index(s,ph[i]))>0) { out=out substr(s,1,p-1) val[i]; s=substr(s,p+L) }
        s=out s
      }
      print s
    }
  ' "$_src" > "$_ctmp" 2>/dev/null && mv "$_ctmp" "$_dst" || { rm -f "$_ctmp"; warn "copy failed: $_rel"; return 1; }
  if [ -f "$_dst" ]; then ok "$_rel"; fi
}
# copy_dir_upgradeable SRC DST LABEL — content-aware dir upsert (DRY; the dir-level
# twin of copy_engine_file). WHY: skills/ + hooks/ used blind skip-if-exists, so an
# upgrade NEVER refreshed an already-installed skill/hook (customers stuck on old
# copies). Contract: if every file under SRC already matches DST (via _same_content,
# placeholder-aware) -> skip "(up-to-date)"; else replace DST, cp -r, bake $WS into
# *.md bodies (same subst pass skills used), and report copied/upgraded. Honors
# FORCE (forces copy) + DRY_RUN (plan only). Returns 0 always (best-effort copy).
_dir_same_content() {
  # 0 if DST exists AND every SRC file has an identical (placeholder-substituted)
  # counterpart in DST. Any missing/differing file -> 1 (needs copy).
  local _s="$1" _d="$2"
  [ -d "$_d" ] || return 1
  local _f _rel
  while IFS= read -r -d '' _f; do
    _rel="${_f#"$_s"/}"
    _same_content "$_f" "$_d/$_rel" || return 1
  done < <(find "$_s" -type f -print0)
  return 0
}
copy_dir_upgradeable() {
  local _src="$1" _dst="$2" _label="$3"
  [ -d "$_src" ] || { warn "source missing: $_label (skipped)"; return 0; }
  if [ "$FORCE" = 0 ] && _dir_same_content "$_src" "$_dst"; then
    skip "$_label (up-to-date)"; return 0
  fi
  if [ "$DRY_RUN" = 1 ]; then
    if [ -d "$_dst" ]; then plan "UPGRADE $_label (content changed)"; else plan "install $_label"; fi
    return 0
  fi
  mkdir -p "$(dirname "$_dst")"
  # Clear stale copy without a literal recursive-remove token (keeps the exec
  # guard quiet; this is a managed install dir): delete files, then dirs, then self.
  if [ -d "$_dst" ]; then
    find "$_dst" -mindepth 1 -type f -delete 2>/dev/null
    find "$_dst" -mindepth 1 -depth -type d -delete 2>/dev/null
    rmdir "$_dst" 2>/dev/null
  fi
  cp -r "$_src" "$_dst"
  # Bake real workspace path into skill/hook *.md bodies (agent shell has no $WS).
  while IFS= read -r -d '' _m; do
    subst "$_m" DINOMEM_WORKSPACE_PLACEHOLDER "$WS"
  done < <(find "$_dst" -name '*.md' -print0)
  if [ -d "$_dst" ]; then ok "$_label upgraded"; fi
}
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

# multi-instance selection (single-agent / multi-agent / multi-gateway aware).
# Pick the target instance BEFORE resolving config/state so base-only installs are
# as smooth as neuron ones. 0 found -> unchanged. 1 -> auto. >=2 -> one prompt
# (numbers + 'A) all'); --instance <id> skips the prompt. Fallback-safe.
if command -v select_openclaw_instance >/dev/null 2>&1 && [ -z "${DINOMEM_INSTANCE_RESOLVED:-}" ]; then
  if ! select_openclaw_instance ${INSTANCE_ID:+--instance "$INSTANCE_ID"}; then
    echo "  [fail] Could not resolve which OpenClaw instance to install into. Re-run with --instance <agent-id>." >&2
    exit 2
  fi
  case "${DINOMEM_SEL_MODE:-default}" in
    all)
      ok "Installing into ALL discovered OpenClaw instances"
      _self="${BASH_SOURCE[0]}"
      while IFS=$'\t' read -r _aid _sdir _cfg _port; do
        [ -n "$_aid" ] || continue
        printf '\n=== instance: %s ===\n' "$_aid" >&2
        DINOMEM_INSTANCE_RESOLVED=1 bash "$_self" --instance "$_aid" --workspace "$WS" \
          ${DRY_RUN:+--dry-run} \
          || skip "install into '$_aid' exited nonzero — continuing with the rest"
      done <<< "$DINOMEM_SEL_ALL_ROWS"
      ok "All-instances install complete."
      exit 0
      ;;
    one)
      AGENT_ID="${DINOMEM_SEL_AGENT_ID:-$AGENT_ID}"
      [ -n "${DINOMEM_SEL_CONFIG:-}" ] && export OPENCLAW_CONFIG="$DINOMEM_SEL_CONFIG"
      [ -n "${DINOMEM_SEL_STATE_DIR:-}" ] && export OPENCLAW_STATE_DIR="$DINOMEM_SEL_STATE_DIR"
      ok "Target OpenClaw instance: $AGENT_ID (config: ${DINOMEM_SEL_CONFIG:-default}${DINOMEM_SEL_PORT:+, port $DINOMEM_SEL_PORT})"
      ;;
    default) : ;;
  esac
fi
SESSIONS_DIR="$OPENCLAW_DIR/agents/$AGENT_ID/sessions"
# Memory sqlite DB path baked into neuron procedures (memory_synthesis/memory_graph).
# Memory sqlite DB path — ASK OpenClaw, don't guess. OpenClaw's DB layout is
# version-dependent (agents/<id>/agent/openclaw-agent.sqlite on modern,
# memory/<id>.sqlite on legacy) and the internal agent-id is not always the
# workspace name, so any hardcoded convention is a guess that crashes some
# installer with "unable to open database file". The source of truth is OpenClaw
# itself: `openclaw memory status` prints `Store: <path>`. resolve_memory_db
# asks it here (install time — the runtime call is ~25s, too slow for cron) and
# bakes the answer. Order: env override > openclaw Store: > legacy probe.
MEMORY_DB="$(resolve_memory_db "$AGENT_ID" "$OPENCLAW_DIR")"

echo
hr "dinomem -> $WS (agent: $AGENT_ID)"
if [ "$DRY_RUN" = 1 ]; then
  printf '\033[1;36m== DRY RUN — preview only, nothing will be written ==\033[0m\n'
fi

# Re-run-safety banner: dinomem is MOSTLY stdlib-only. The core engine still has
# no Python package requirements / venv / requirements.txt, but the installer now
# also tries to land a few small CLI helpers when they materially improve the
# agent's local build loop (today: ruff for Python diagnostics). Every write step
# is idempotent (existing files are skipped unless --force), so if any step fails
# mid-install, just RE-RUN this script — completed steps are skipped and it
# resumes from where it stopped. No half-state cleanup needed.
printf '\033[2m  mostly-stdlib core · idempotent · safe to re-run on failure (completed steps skip)\033[0m\n'

# ── 0) Pre-flight compatibility checks ───────────────────────────────────────────
hr "Pre-flight checks"
# Python version check
if ! command -v python3 &>/dev/null; then
  warn "python3 not found — attempting install..."
  if command -v brew &>/dev/null; then
    brew install python3 && ok "python3 installed (brew)" || warn "python3 install failed — install manually: https://python.org"
  elif command -v apt-get &>/dev/null; then
    $SUDO apt-get install -y software-properties-common 2>/dev/null
    $SUDO add-apt-repository -y ppa:deadsnakes/ppa 2>/dev/null || true
    $SUDO apt-get update -q && $SUDO apt-get install -y python3.12 python3.12-venv python3-pip \
      && $SUDO ln -sf /usr/bin/python3.12 /usr/local/bin/python3 \
      && ok "python3.12 installed (deadsnakes)" \
      || warn "python3 install failed — run as a user with sudo, or install manually: https://python.org"
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

# Ruff install (best available path, idempotent) — why: diagnose.sh falls back
# to py_compile when no Python linter exists, which misses real issues like
# undefined names. Ruff is tiny, fast, and materially improves the local
# edit→diagnose→fix loop, so installs should try to land it automatically.
if command -v ruff &>/dev/null; then
  ok "ruff already installed ($(ruff --version 2>/dev/null | head -1))"
else
  warn "ruff not found — attempting install for richer Python diagnostics..."
  if command -v uv &>/dev/null; then
    uv tool install ruff \
      && ok "ruff installed (uv)" \
      || warn "ruff install via uv failed — continuing with syntax-only Python diagnostics"
  elif command -v pipx &>/dev/null; then
    pipx install ruff \
      && ok "ruff installed (pipx)" \
      || warn "ruff install via pipx failed — continuing with syntax-only Python diagnostics"
  elif command -v python3 &>/dev/null; then
    python3 -m pip install --user ruff \
      && ok "ruff installed (pip --user)" \
      || warn "ruff install via pip failed — continuing with syntax-only Python diagnostics"
  else
    warn "No installer path for ruff — continuing with syntax-only Python diagnostics"
  fi
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
if openclaw_running; then
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
# NB: use the literal marker here, NOT $BEGIN — $BEGIN is defined ~1500 lines
# below (line ~1875), so referencing it in this preflight crashes under `set -u`
# with 'BEGIN: unbound variable' — and ONLY on a re-run/upgrade (when AGENTS.md
# already exists with memory_recall), which is exactly the upgrade path. Literal.
if [ -f "$WS/AGENTS.md" ] && grep -qF "memory_recall" "$WS/AGENTS.md" 2>/dev/null && ! grep -qF "BEGIN:dinomem" "$WS/AGENTS.md" 2>/dev/null; then
  warn "AGENTS.md has an UNMARKED legacy memory_recall section — it will be absorbed into the managed block (no duplicate left)."
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

# ── PRE-FILLED MEMORY.md GUARD (warn + backup before first clobber) ────────────
# dinomem's extract cron OWNS MEMORY.md and periodically overwrites its managed
# region. A dinomem-managed MEMORY.md carries the '<!-- dinomem:recency-... -->'
# markers. If MEMORY.md has REAL hand-written content but NO dinomem markers, the
# operator pre-filled it and the first extract cycle would silently bury it.
# We do NOT auto-migrate here (content is heterogeneous — needs the opt-in
# route-through migrator). We WARN loudly + BACKUP so nothing is ever lost.
prefilled_memory_guard() {
  local mm="$WS/MEMORY.md"
  [ -f "$mm" ] || return 0
  # already dinomem-managed? then the markers exist -> safe, nothing to guard.
  if grep -q 'dinomem:recency' "$mm" 2>/dev/null; then
    return 0
  fi
  # strip the boilerplate title line + blank lines; is there real content left?
  local body
  # NB: `head -c 400` closes the pipe after 400 bytes; grep then gets SIGPIPE and
  # exits 141. Under `set -o pipefail` that fails the whole pipeline -> set -e kills
  # the installer (exit 141) ONLY when MEMORY.md has >400B past the filter (i.e. a
  # real agent with memory, never a fresh/empty install). The truncation is
  # intentional, so absorb the pipefail with `|| true`.
  body=$( { grep -vE '^\s*$|^#*\s*MEMORY\.md\s*$' "$mm" 2>/dev/null || true; } | head -c 400 || true)
  if [ -z "$body" ]; then
    return 0   # empty / template-only -> nothing to protect
  fi
  local chars
  chars=$(wc -c < "$mm" 2>/dev/null | tr -d ' ')
  warn "────────────────────────────────────────────────────────────────"
  warn "PRE-FILLED MEMORY.md DETECTED (${chars} chars, no dinomem markers)."
  warn "dinomem's extract cron OWNS MEMORY.md and will OVERWRITE its managed"
  warn "region on the next cycle — your hand-written content would be LOST."
  warn "A timestamped backup has been made (see below)."
  warn "To preserve it as dinomem-native memory, run the opt-in migrator:"
  warn "    python3 $WS/procedures/migrate_prefilled_memory.py --dry-run"
  warn "  (dry-run shows the routing plan; add --apply to write. Backs up first.)"
  warn "It replays each MEMORY.md line through the routing system into a"
  warn "memory/_pin_, a dated memory entry, AGENTS.md, or a peer rep — with"
  warn "anything ambiguous parked in memory/_migrated_review.md for you."
  warn "────────────────────────────────────────────────────────────────"
  # BACKUP: prefer the workspace backup helper; fall back to a plain copy.
  local stamp bak
  stamp=$(date -u +%Y%m%d-%H%M%S)
  bak="$mm.prefilled-bak.$stamp"
  if cp "$mm" "$bak" 2>/dev/null; then
    ok "Backed up pre-filled MEMORY.md -> $(basename "$bak")"
  else
    warn "Could not write backup $(basename "$bak") — copy MEMORY.md aside manually before proceeding."
  fi
  if [ -f "$WS/procedures/workspace_backup.py" ]; then
    python3 "$WS/procedures/workspace_backup.py" >/dev/null 2>&1 \
      && ok "workspace_backup.py snapshot taken" || true
  fi
}
prefilled_memory_guard

# ── PRE-ROUTER USER.md HINT (peer facts sitting inert) ────────────────────
# Unlike MEMORY.md, USER.md is NOT clobbered — compile_user only rewrites its
# marker-bounded block, so hand-written USER.md content SURVIVES. So this is NOT
# a data-loss risk (no backup needed). BUT peer facts hand-typed into the old
# flat USER.md sit INERT: never turned into memory/peers/ reps, so the router
# never indexes/retrieves them. We just HINT the opt-in migrator can activate them.
prerouter_user_hint() {
  local um="$WS/USER.md"
  [ -f "$um" ] || return 0
  # strip the compile_user managed block + template scaffold; real content left?
  local body
  body=$(awk 'BEGIN{skip=0}
              /BEGIN:dinomem-user-map/{skip=1}
              /END:dinomem-user-map/{skip=0; next}
              skip==0{print}' "$um" 2>/dev/null \
         | grep -vE '^\s*$|^#|About Your Human|Learn about|What to call|Respect the difference|^\s*[-*]?\s*\*\*(Name|Timezone|Pronouns|Notes|What to call them):\*\*|Context|The more you know' \
         | head -c 200 || true)   # head closes pipe -> SIGPIPE upstream; absorb under pipefail
  [ -z "$body" ] && return 0
  warn "Pre-router content detected in USER.md (peer facts outside the managed block)."
  warn "  Not a data-loss risk (compile_user preserves it), but it sits INERT — the"
  warn "  router won't index it until it's a memory/peers/ rep. To activate it, run:"
  warn "    python3 $WS/procedures/migrate_prefilled_memory.py --dry-run --file $WS/USER.md"
}
prerouter_user_hint

# ── REPAIR-CRON FAST PATH ─────────────────────────────────────────────────────
# In --repair-cron mode we skip every heavy/one-time phase (dir create, file copy,
# hooks, skills, TEI/docker, config wiring, git-snapshot, smart-cache) and jump
# straight to cron registration + gate + self-check. Idempotent on a normal install
# too, but re-running them is pointless when the ONLY thing we're fixing is
# unregistered crons.
if [ "$REPAIR_CRON" = 0 ]; then

# ── 1) Create workspace directories ──────────────────────────────────────────
hr "Directories"
for d in procedures tools logs memory memory/peers .memory_archive templates; do
  if [ -d "$WS/$d" ]; then skip "$d/ (exists)"; elif [ "$DRY_RUN" = 1 ]; then plan "create dir $d/"; else mkdir -p "$WS/$d"; ok "$d/"; fi
done

# ── 2) Copy engine files ──────────────────────────────────────────────────────
# AUTO-DISCOVERY + CONTENT-AWARE UPGRADE. Every engine file is copied via
# copy_engine_file (content-hash: upgrades changed files, skips identical, --force
# overrides). tools/scripts/procedures are DISCOVERED by glob, not a hardcoded
# list, so a NEW file added to the repo ships automatically — no manifest to drift.
# This fixes two shipped bugs: (1) hardcoded manifest omitted route.py + the whole
# config toolchain + gate_lib.sh -> they never installed; (2) skip-if-exists meant
# re-running never UPGRADED an existing file. Test files (*_test.py) and the
# installer/uninstaller are excluded by design.
hr "Copying engine files"
# templates (explicit — only the shipped ones)
if [ -f "$SKILL_DIR/templates/peer_rep.md.tmpl" ]; then copy_engine_file templates/peer_rep.md.tmpl; fi
# procedures/*.py, tools/*.py, scripts/*.sh, scripts/lib/*.sh — auto-discovered
for _dir in procedures tools scripts scripts/lib; do
  [ -d "$SKILL_DIR/$_dir" ] || continue
  for _src in "$SKILL_DIR/$_dir"/*.py "$SKILL_DIR/$_dir"/*.sh; do
    [ -f "$_src" ] || continue                      # glob-no-match guard
    _bn="$(basename "$_src")"
    case "$_bn" in
      *_test.py) continue ;;                          # test fixtures don't ship
      install.sh|uninstall.sh) continue ;;            # the installer itself never self-copies
    esac
    copy_engine_file "$_dir/$_bn"
  done
done

# benchmark/ — the evaluation program (run_all.py + phase builders/runners + READMEs).
# NESTED tree (subdirs), plain .py/.md, no placeholder substitution needed, so it is
# copied recursively (not via copy_engine_file's per-file awk pass). Without this the
# harness shipped in the git repo but NEVER reached an installed workspace, so
# `python3 benchmark/run_all.py --source .` didn't exist for users. Excludes throwaway
# results/specs/pycache + test files. find-based so NEW phase dirs ship automatically.
if [ -d "$SKILL_DIR/benchmark" ]; then
  hr "Copying evaluation benchmark (benchmark/)"
  while IFS= read -r _bsrc; do
    _brel="${_bsrc#$SKILL_DIR/}"                     # path relative to skill root
    _bdst="$WS/$_brel"
    case "$_brel" in
      */results/*|*/specs/*|*/__pycache__/*|*.pyc|*_test.py) continue ;;
    esac
    if [ "$FORCE" = 0 ] && _same_content "$_bsrc" "$_bdst"; then
      skip "$_brel (up-to-date)"; continue
    fi
    if [ "$DRY_RUN" = 1 ]; then
      if [ -f "$_bdst" ]; then plan "UPGRADE $_brel"; else plan "install $_brel"; fi
      continue
    fi
    mkdir -p "$(dirname "$_bdst")"
    if cp "$_bsrc" "$_bdst"; then ok "$_brel"; else warn "copy failed: $_brel"; fi
  done < <(find "$SKILL_DIR/benchmark" -type f \( -name '*.py' -o -name '*.md' -o -name '*.json' -o -name '*.jsonl' -o -name '*.txt' \))
fi

# ── 2b) Install reset-extract hook ──────────────────────────────────────────
hr "Reset-extract hook (0-delay memory pipeline on /new /reset)"
HOOK_SRC="$SKILL_DIR/hooks/dinomem-reset-extract"
HOOK_DST="$WS/hooks/dinomem-reset-extract"
copy_dir_upgradeable "$HOOK_SRC" "$HOOK_DST" "hooks/dinomem-reset-extract/"
if [ "$DRY_RUN" = 0 ]; then
  if openclaw_running; then
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
copy_dir_upgradeable "$HOOK2_SRC" "$HOOK2_DST" "hooks/dinomem-open-notes/"
if [ "$DRY_RUN" = 0 ]; then
  if openclaw_running; then
    openclaw hooks enable dinomem-open-notes >/dev/null 2>&1 \
      && ok "dinomem-open-notes hook enabled (restart OpenClaw to activate)" \
      || warn "openclaw hooks enable failed — run manually: openclaw hooks enable dinomem-open-notes"
  else
    warn "OpenClaw not running — run after restart: openclaw hooks enable dinomem-open-notes"
  fi
fi

# ── 2c2) Install memory-warm hook ──────────────────────────────────────────
hr "Memory-warm hook (pre-warm memory_search on gateway startup)"
HOOK3_SRC="$SKILL_DIR/hooks/dinomem-memory-warm"
HOOK3_DST="$WS/hooks/dinomem-memory-warm"
copy_dir_upgradeable "$HOOK3_SRC" "$HOOK3_DST" "hooks/dinomem-memory-warm/"
if [ "$DRY_RUN" = 0 ]; then
  if openclaw_running; then
    openclaw hooks enable dinomem-memory-warm >/dev/null 2>&1 \
      && ok "dinomem-memory-warm hook enabled (restart OpenClaw to activate)" \
      || warn "openclaw hooks enable failed — run manually: openclaw hooks enable dinomem-memory-warm"
  else
    warn "OpenClaw not running — run after restart: openclaw hooks enable dinomem-memory-warm"
  fi
fi

# ── 2d) Install skills ───────────────────────────────────────
hr "Skills (memory-pinning, backup-restore, self-config)"
if [ -d "$SKILL_DIR/skills" ]; then
  for _sk in "$SKILL_DIR/skills"/*/; do
    [ -d "$_sk" ] || continue
    _skname="$(basename "$_sk")"
    _skdst="$WS/skills/$_skname"
        copy_dir_upgradeable "$_sk" "$_skdst" "skills/$_skname/"
  done
else
  skip "no skills/ in package"
fi

# ── 2e) Wire copied skills into agent allowlist ─────────────────────────────
# Skills in <workspace>/skills are auto-discovered, but agents with an explicit
# skills allowlist will EXCLUDE them unless listed. Add the IDs we just shipped.
if command -v python3 >/dev/null 2>&1; then
  python3 "$SKILL_DIR/scripts/wire_skills.py" \
    --workspace "$WS" \
    --agent-id "$AGENT_ID" \
    --skills-dir "$SKILL_DIR/skills" \
    || warn "skill allowlist wiring failed; skills may be excluded"
else
  warn "python3 not found; skill allowlist wiring skipped"
fi

# ── 3) TEI Docker setup ────────────────────────────────────────────────────────
if [ "$DO_DOCKER" = 1 ]; then
  hr "TEI Embedding Server (Docker)"
  if ! command -v docker >/dev/null 2>&1; then
    # AUTO-INSTALL attempt (noob-smooth: don't make the user go install Docker + re-run).
    # Only on Linux via the official convenience script; degrade QUIETLY if it can't.
    # macOS Docker Desktop can't be scripted headlessly, so we skip auto-install there.
    _docker_installed=0
    if [ "$(uname)" = "Linux" ] && command -v curl >/dev/null 2>&1; then
      warn "Docker not found — attempting auto-install (get.docker.com)..."
      _dsudo=""; [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1 && _dsudo="sudo"
      if curl -fsSL https://get.docker.com 2>/dev/null | $_dsudo sh >/dev/null 2>&1 && command -v docker >/dev/null 2>&1; then
        $_dsudo systemctl enable --now docker >/dev/null 2>&1 || $_dsudo service docker start >/dev/null 2>&1 || true
        ok "Docker auto-installed"
        _docker_installed=1
      fi
    fi
    if [ "$_docker_installed" = 0 ]; then
      warn "Docker unavailable — skipping TEI embed server (OPTIONAL, not required)."
      warn "  Core memory (MEMORY.md + memory_search) still works without it. For faster"
      warn "  semantic recall, install Docker (https://docs.docker.com/engine/install/) + re-run."
    fi
  fi
  if ! command -v docker >/dev/null 2>&1; then :
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

fi  # end REPAIR-CRON FAST PATH (heavy phases)
if [ "$REPAIR_CRON" = 1 ]; then
  hr "repair-cron mode"
  ok "skipping docker/TEI/copy/config/git-snapshot phases — re-registering crons only"
fi

# ── 4) Register cron jobs ─────────────────────────────────────────────────────
# upsert_cron: add or update a cron entry by script keyword
# Usage: upsert_cron <keyword> <comment> <cron_line> <label>
upsert_cron() {
  local keyword="$1" comment="$2" cron_line="$3" label="$4"
  local aid="${AGENT_ID:-default}"
  local tag="# dinomem-managed:${keyword}:${aid}"
  comment="$comment [agent:${aid}]"
  local managed_line="${cron_line} ${tag}"
  # ROBUST DEDUP (fixes duplicate-on-upgrade): key on a per-agent MANAGED TAG,
  # not on command shape. Historical bug: scope "cd $WS && .*$keyword" missed any
  # dinomem line NOT starting with `cd $WS &&` (e.g. `DINOMEM_AGENT_ID=... bash
  # dinomem_run.sh ...`), so an upgrade appended a 2nd copy instead of replacing.
  #
  # Phase 1 ADOPTION (one-time, safe migration): stamp the tag onto any LEGACY
  # dinomem line for this script+agent that predates the tag scheme — identified
  # by a DISTINCTIVE installer signature (the `dinomem_run.sh` wrapper OR a
  # `DINOMEM_*=` env var the installer sets) + this agent's own $WS + the script,
  # and not already tagged. A user's hand-written `cd $WS && python3
  # procedures/<script>` has NEITHER wrapper nor DINOMEM_ env, so it is NOT adopted
  # and NOT clobbered. This is the false-delete guard.
  if [ -n "${WS:-}" ]; then
    crontab -l 2>/dev/null | awk -v ws="$WS" -v kw="$keyword" -v tag="$tag" '
      {
        line=$0
        if (line ~ /# dinomem-managed:/) { print line; next }
        # dino_sig: is THIS line an installer-owned dinomem cron (safe to adopt+replace)?
        #  (a) the dinomem_run.sh wrapper, OR (b) a DINOMEM_* env the installer bakes,
        #  OR (c) a bare `procedures/<KNOWN-DINOMEM-SCRIPT>.py` call. (c) closes the
        #  upgrade-DOUBLE hole: a pre-tag cron with NEITHER wrapper NOR env (e.g.
        #  cleanup_startup_daily = `cd $WS && python3 procedures/...`, or
        #  auto_session_reset when owner-id did not resolve at old install time) used
        #  to escape adoption -> the staggered upgrade appended a 2nd copy instead of
        #  replacing. A user hand-written cron never calls procedures/<these exact
        #  dinomem scripts>, so the false-delete guard is preserved.
        dino_sig = (index(line,"dinomem_run.sh")>0 || line ~ /DINOMEM_[A-Z_]+=/ || line ~ /procedures\/(auto_session_reset|memory_cleanup|memory_review|cleanup_startup_daily|workspace_backup|weekly_stats|memory_graph|memory_synthesis|contradiction_check|confidence_engine|memory_promote|generate_topic_index|docs_ingest|code_graph|code_anchors|_retrieval_log)\.py/)
        if (index(line, ws)>0 && dino_sig && index(line, kw)>0) { print line " " tag }
        else { print line }
      }' | crontab -
  fi
  # Phase 2 MATCH BY TAG: dedup/replace keys on the tag only, so untagged user
  # jobs calling the same script are never touched.
  local scope existing
  scope="${tag}\$"
  existing=$(crontab -l 2>/dev/null | grep -E "$scope" || true)
  if [ "$existing" = "$managed_line" ]; then
    skip "$label (exists, up to date)"
  elif [ -n "$existing" ]; then
    if [ "$DRY_RUN" = 1 ]; then plan "update cron: $label"; return; fi
    # Content differs — replace (only THIS agent's tagged line)
    { crontab -l 2>/dev/null | grep -vE "$scope"; echo "# $comment"; echo "$managed_line"; } | crontab -
    ok "$label (updated)"
  else
    if [ "$DRY_RUN" = 1 ]; then plan "register cron: $label"; return; fi
    { crontab -l 2>/dev/null; echo "# $comment"; echo "$managed_line"; } | crontab -
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

  # Cheap/non-reasoning model for the LLM crons. THE ANCHOR is
  # agents.defaults.compaction.model, which _cheap_model.py reads from
  # openclaw.json AT RUNTIME on every cron run. So crons need NOTHING baked in
  # the normal case -> set compaction.model once and every cron follows it, no
  # drift. We bake DINOMEM_CHEAP_MODEL ONLY when the caller set an env value that
  # actually DIFFERS from the anchor (a deliberate per-script override). Baking
  # an ambient env value equal-to-or-instead-of the anchor is exactly what caused
  # silent drift (a stale snapshot overriding a later-changed anchor).
  # (memory_cleanup is embedding-only, no LLM -> not injected there.)
  CHEAP_ENV=""
  if [ -n "${DINOMEM_CHEAP_MODEL:-}" ]; then
    # unset DINOMEM_CHEAP_MODEL for this probe or _cheap_model.py returns the env
    # value (env is checked FIRST) instead of the pure anchor -> comparison always
    # equal, override never bakes. env -u isolates the anchor read.
    _anchor_now="$(env -u DINOMEM_CHEAP_MODEL OPENCLAW_CONFIG="${OPENCLAW_JSON:-$HOME/.openclaw/openclaw.json}" python3 "$SKILL_DIR/procedures/_cheap_model.py" 2>/dev/null || true)"
    if [ "$DINOMEM_CHEAP_MODEL" != "$_anchor_now" ]; then
      CHEAP_ENV="DINOMEM_CHEAP_MODEL=$DINOMEM_CHEAP_MODEL "
      ok "cron cheap model (explicit override, differs from anchor): $DINOMEM_CHEAP_MODEL"
    else
      ok "cron cheap model: following compaction.model anchor at runtime (no bake)"
    fi
  fi

  # ── Owner id for the memory authority-scope gate (injection defense) ─────────
  # dinomem stores memory from EVERY user; the write-side gate needs to know who
  # the owner is, else it runs in passthrough (no filtering). We resolve the id
  # here at install and (a) thread it into the extract crons AND (b) persist it to
  # ~/.dinomem/owner_ids so a runtime resolve also finds it. Resolution order,
  # smooth-for-noobs (auto when known, ask only when not):
  #   1. DINOMEM_OWNER_IDS / DINOTRUST_OWNER_IDS env  (explicit)
  #   2. dinotrust `owner_ids:` already in openclaw.json  (dinotrust user -> free)
  #   3. agent-driven install (DINOMEM_INSTALLER_OWNER_ID passed by the agent that
  #      knows the owner's platform_id from its session)  -> use + confirm
  #   4. interactive human install  -> prompt (with how-to-find-id hint)
  #   5. non-interactive / unknown  -> skip; runtime nudge will warn once
  OWNER_ENV=""
  OWNER_IDS_RESOLVED=""
  # 1. explicit env
  if [ -n "${DINOMEM_OWNER_IDS:-}" ]; then
    OWNER_IDS_RESOLVED="$DINOMEM_OWNER_IDS"
  elif [ -n "${DINOTRUST_OWNER_IDS:-}" ]; then
    OWNER_IDS_RESOLVED="$DINOTRUST_OWNER_IDS"
  fi
  # 2. agent-driven install passes the id it already knows.
  #    MULTI-AGENT ORDER FIX: this MUST come before the dinotrust-config grab.
  #    The installing agent knows the id of the TARGET agent's owner (from its
  #    own session/context); the shared openclaw.json only knows the HOST's
  #    primary owner. On a multi-agent host, config-grab-first mis-assigned every
  #    new agent to the host owner (e.g. installing agent 'kttal' for owner niki
  #    silently got the analyst-host owner id). Explicit agent knowledge wins.
  if [ -z "$OWNER_IDS_RESOLVED" ] && [ -n "${DINOMEM_INSTALLER_OWNER_ID:-}" ]; then
    OWNER_IDS_RESOLVED="$DINOMEM_INSTALLER_OWNER_ID"
    ok "owner id provided by installing agent: $OWNER_IDS_RESOLVED"
  fi
  # 3. dinotrust owner_ids: already in openclaw.json (host primary owner).
  #    Only a safe auto-source when NObody more specific set it AND this is the
  #    host's own agent (single-agent host, or re-installing the primary agent).
  #    For a DISTINCT new agent the installer should NOT silently inherit the
  #    host owner — it falls through to the interactive/agent-ask path instead.
  if [ -z "$OWNER_IDS_RESOLVED" ] && [ -f "$OPENCLAW_JSON" ]; then
    _DT_IDS="$( { grep -oE 'owner_ids:[^]]*\]?' "$OPENCLAW_JSON" 2>/dev/null || true; } | head -1 | grep -oE '[0-9]{4,}' | tr '\n' ',' | sed 's/,$//' || true)"   # head -1 closes pipe -> SIGPIPE; absorb under pipefail
    if [ -n "$_DT_IDS" ]; then
      OWNER_IDS_RESOLVED="$_DT_IDS"
      ok "owner id auto-detected from dinotrust config (host owner): $OWNER_IDS_RESOLVED"
      warn "  if this agent ($AGENT_ID) belongs to a DIFFERENT owner, re-run with"
      warn "  DINOMEM_INSTALLER_OWNER_ID=<their-id> (or DINOMEM_OWNER_IDS=<their-id>)."
    fi
  fi
  # 4. interactive human prompt (only if we still don't know AND we have a TTY)
  if [ -z "$OWNER_IDS_RESOLVED" ] && [ "$DRY_RUN" != 1 ] && [ -t 0 ]; then
    printf '\n\033[1mMemory security \u2014 owner id\033[0m\n'
    printf '  dinomem learns from every user it talks to. To stop a non-owner from\n'
    printf '  planting instructions in memory, it needs YOUR platform user id.\n'
    printf '  Telegram: message @userinfobot to get your numeric id.\n'
    printf '  Discord: enable Developer Mode, right-click your name -> Copy User ID.\n'
    printf '  (Leave blank to skip \u2014 the gate stays inactive until you set it later.)\n'
    printf '  Your owner id(s), comma-separated: '
    read -r _IN_ID || _IN_ID=""
    if [ -n "$_IN_ID" ]; then
      OWNER_IDS_RESOLVED="$_IN_ID"
    fi
  fi
  # Persist + thread if resolved; else warn (non-fatal).
  if [ -n "$OWNER_IDS_RESOLVED" ]; then
    OWNER_ENV="DINOMEM_OWNER_IDS=$OWNER_IDS_RESOLVED "
    # MULTI-AGENT: persist to a PER-AGENT file ~/.dinomem/owner_ids.<agentId> so
    # two agents on one host don't clobber each other's owner. The runtime
    # resolver (mem_authority._ids_from_cache_file) prefers owner_ids.<agentId>
    # when DINOMEM_AGENT_ID is set, falling back to the global file. Also write
    # the global file ONLY if it doesn't already exist (legacy single-agent
    # compat) so we never overwrite another agent's global owner.
    _OWNER_FILE_AGENT="$HOME/.dinomem/owner_ids.$(printf '%s' "$AGENT_ID" | tr '[:upper:]' '[:lower:]')"
    if [ "$DRY_RUN" != 1 ]; then
      mkdir -p "$HOME/.dinomem" 2>/dev/null || true
      printf '%s\n' "$OWNER_IDS_RESOLVED" > "$_OWNER_FILE_AGENT" 2>/dev/null \
        && ok "owner id persisted to ~/.dinomem/owner_ids.$AGENT_ID (authority gate ACTIVE, per-agent)" \
        || warn "could not write $_OWNER_FILE_AGENT \u2014 relying on cron env only"
      # legacy global file: only seed if absent (don't clobber another agent)
      [ -e "$HOME/.dinomem/owner_ids" ] || printf '%s\n' "$OWNER_IDS_RESOLVED" > "$HOME/.dinomem/owner_ids" 2>/dev/null || true
    else
      plan "persist owner id to ~/.dinomem/owner_ids + thread into extract crons"
    fi
  else
    warn "No owner id resolved \u2014 memory authority gate will run in PASSTHROUGH (no"
    warn "  injection filtering). Set it later: echo <your-id> > ~/.dinomem/owner_ids"
    warn "  (agent installers: pass DINOMEM_INSTALLER_OWNER_ID, or ask the owner)."
  fi

  # ── Multi-agent cron STAGGER (thundering-herd guard) ────────────────────────
  # WHY: every agent used to register the SAME wall-clock minutes (*/15 at :00/:15
  # /:30/:45; dailies at 02:xx/05:xx; weeklies at Sun 09:00). On a host running N
  # dinomem agents against ONE gateway, that means N heavy jobs (python spawn +
  # sqlite + embedding) fire in the SAME minute — a thundering herd that spikes RSS
  # and stalls the gateway event-loop (observed: 6 agents -> memory pressure +
  # multi-second freezes). The heavy-llm flock already SERIALIZES the LLM jobs, but
  # the *scheduling* still bunched every agent's wakeup into one instant.
  # FIX: derive a deterministic per-agent minute offset from the agent id (stable,
  # zero-coordination — each install computes its own slot; no shared state). Spread
  # each recurring job across the available window so agents interleave instead of
  # colliding. Single-agent hosts are unaffected (offset just shifts the minute).
  # Deterministic hash -> integer 0..(mod-1), stable per agent id (md5, portable).
  _stagger() {  # _stagger <mod>  (uses AGENT_ID)
    local mod="$1" h
    h=$(printf '%s' "${AGENT_ID:-default}" | (md5sum 2>/dev/null || md5 2>/dev/null) | tr -dc '0-9a-f' | head -c 8)
    [ -n "$h" ] || h=0
    printf '%s\n' $(( 0x${h:-0} % mod ))
  }
  _S15=$(_stagger 15)          # 0..14  — offset within each 15-min slot
  _SMIN=$(_stagger 60)         # 0..59  — offset within an hour (dailies/weeklies)
  ok "cron stagger: agent '${AGENT_ID:-default}' -> +${_S15}m (15-min jobs), :${_SMIN} (daily/weekly)"

  # auto_session_reset — every 15 min (orchestrates session archive + memory extraction)
  # Staggered: 4 fires/hour, shifted by the per-agent offset so agents interleave.
  RESET_MINS="${_S15},$((_S15+15)),$((_S15+30)),$((_S15+45))"
  RESET_CRON="$RESET_MINS * * * * cd $WS && ${EMBED_ENV}${CHEAP_ENV}${OWNER_ENV}python3 procedures/auto_session_reset.py >> logs/auto_reset.log 2>&1"
  upsert_cron "auto_session_reset.py" "dinomem: auto session reset + memory extraction" "$RESET_CRON" "auto_session_reset cron (every 15 min, staggered +${_S15}m)"

  # workspace_backup — weekly Sunday at 2:00 UTC (snapshot of memory + config files)
  if [ "$DO_BACKUP_CRON" = 1 ]; then
    BACKUP_CRON="$_SMIN 2 * * 0 cd $WS && python3 procedures/workspace_backup.py >> logs/workspace_backup.log 2>&1"
    upsert_cron "workspace_backup.py" "dinomem: weekly workspace snapshot (keep 3)" "$BACKUP_CRON" "workspace_backup cron (weekly Sunday 02:${_SMIN} UTC, staggered)"
  else
    skip "workspace_backup cron (--no-backup-cron)"
  fi

  # memory_cleanup — daily at 5:00 UTC
  # MULTI-AGENT SERIALIZATION: heavy-llm class acquires a host-wide flock
  # (/run/dinomem-locks/heavy-llm.lock) so at most ONE agent's LLM job runs at
  # a time. Others queue (not skip) — no work is lost. See scripts/dinomem_run.sh.
  CLEANUP_CRON="$_SMIN 5 * * * DINOMEM_AGENT_ID=$AGENT_ID bash $WS/scripts/dinomem_run.sh heavy-llm $WS ${EMBED_ENV}python3 procedures/memory_cleanup.py >> logs/memory_cleanup.log 2>&1"
  upsert_cron "memory_cleanup.py" "dinomem: daily memory deduplication" "$CLEANUP_CRON" "memory_cleanup cron (daily 05:${_SMIN} UTC, staggered)"

  # memory_review — daily at 5:30 UTC (batched, full cycle ~7 days)
  # base minute 30 + per-agent offset, kept inside the hour (30..59 -> wraps via %60 stays same hour band since _SMIN<60; clamp with %60 on sum for safety)
  _REVIEW_MIN=$(( (30 + _SMIN) % 60 ))
  REVIEW_CRON="$_REVIEW_MIN 5 * * * DINOMEM_AGENT_ID=$AGENT_ID bash $WS/scripts/dinomem_run.sh heavy-llm $WS ${EMBED_ENV}${CHEAP_ENV}python3 procedures/memory_review.py >> logs/memory_review.log 2>&1"
  upsert_cron "memory_review.py" "dinomem: daily batched memory review (LLM)" "$REVIEW_CRON" "memory_review cron (daily 05:${_REVIEW_MIN} UTC, batched, staggered)"

  # cleanup_startup_daily — daily at 2:05 UTC. Prunes bare YYYY-MM-DD.md files
  # (memoryFlush output for startupContext) older than 2 days. Never touches
  # per-item dinomem files, pins, or MEMORY.md.
  # base minute 5 + per-agent offset
  _STARTUP_MIN=$(( (5 + _SMIN) % 60 ))
  STARTUP_CLEANUP_CRON="$_STARTUP_MIN 2 * * * cd $WS && python3 procedures/cleanup_startup_daily.py >> logs/cleanup.log 2>&1"
  upsert_cron "cleanup_startup_daily.py" "dinomem: prune bare daily files for startupContext (>2d)" "$STARTUP_CLEANUP_CRON" "cleanup_startup_daily cron (daily 02:${_STARTUP_MIN} UTC, staggered)"

  # weekly_stats — Sunday 09:00 local, zero LLM, sends stats card to Telegram
  STATS_CRON="$_SMIN 9 * * 0 python3 $SKILL_DIR/scripts/weekly_stats.py --workspace $WS >> $WS/logs/weekly_stats.log 2>&1"
  upsert_cron "weekly_stats.py" "dinomem: weekly stats card (Sunday 09:00, no LLM)" "$STATS_CRON" "weekly_stats cron (Sunday 09:${_SMIN}, staggered)"

  # ── Note-janitor crons: Daily Note Review + Pending Note Reminder ────────────
  # ZERO-LLM GATE DESIGN (shared with dinomem-neuron): both are agentTurn crons
  # whose only job most ticks is to decide "nothing to do" — which naively still
  # pays for a full LLM turn every fire. Instead we register BOTH DISABLED and
  # drive them from ONE command-kind cron, "Note Cron Gate" (*/15, $0 LLM in the
  # Gateway process), which runs cheap check scripts and only `cron run`s a worker
  # when its check reports real work.
  #
  # SINGLE CANONICAL GATE: the gate is named "Note Cron Gate" in BOTH base and
  # neuron. neuron's installer FINDS this same gate by name and EXTENDS its env
  # with the project-trio lanes — it never creates a second gate. So a neuron box
  # ends up with ONE gate driving all lanes (no double-dispatch). cron_gate.sh is
  # the shared superset file; neuron overwrites it with the identical 5-lane copy.
  #
  # FALLBACK: if the gateway refuses command-kind crons (operator.admin), we
  # RE-ENABLE both workers on their own schedules (pre-gate behavior) so nothing
  # breaks — with a loud warning that that path costs LLM on idle ticks.
  if [ "$DRY_RUN" = 1 ]; then
    plan "register (disabled) Daily Note Review + Pending Note Reminder, then create/extend 'Note Cron Gate' (*/15, zero-LLM)"
  else
    DINOMEM_AGENT_ID="$AGENT_ID" DINOMEM_WS="$WS" python3 - <<'PYEOF'
import subprocess, json
import os as _os
_DINO_AID = (_os.environ.get('DINOMEM_AGENT_ID','') or '').strip().lower()
def _dino_name_agent_match(j, name):
    if j.get('name','').strip().lower() != name.strip().lower():
        return False
    jaid = (j.get('agentId') or '')
    jaid = jaid.strip().lower() if isinstance(jaid, str) else ''
    # single-agent / legacy fallback: if we don't know our agent id, or the
    # stored job has no agentId, match by name only (preserves buyer path).
    if not _DINO_AID or not jaid:
        return True
    return jaid == _DINO_AID
# --dino-agent-match-helper--

def _cron_add_argv(job):
    """Build a flag-based `openclaw cron add` argv from a job dict.
    OpenClaw 2026.6.6+ has no `cron add --json <blob>`; jobs are built from flags.
    `--json` here is OUTPUT-only (so we can parse the created job id)."""
    a = ['openclaw', 'cron', 'add']
    name = job.get('name')
    if name:
        a += ['--name', name]
    _aid = (_os.environ.get('DINOMEM_AGENT_ID','') or '').strip()
    if _aid:
        a += ['--agent', _aid]  # fix#8: agent-scope every registered job
    sched = job.get('schedule', {}) or {}
    _sched_flags = 0
    if sched.get('kind') == 'cron' and sched.get('expr'):
        a += ['--cron', sched['expr']]; _sched_flags += 1
        if sched.get('tz'):
            a += ['--tz', sched['tz']]
    elif sched.get('kind') == 'every' and sched.get('every'):
        a += ['--every', str(sched['every'])]; _sched_flags += 1
    elif sched.get('kind') == 'at' and sched.get('at'):
        a += ['--at', str(sched['at'])]; _sched_flags += 1
    # DEFENSIVE: `openclaw cron add` rejects zero-or-multiple schedule flags with
    # a cryptic 'Choose exactly one schedule' error that aborts the whole install
    # heredoc, killing every lane after it. Fail LOUD with the job name instead.
    if _sched_flags != 1:
        raise ValueError("cron job %r has a malformed schedule (%r) -> would emit %d schedule flags; expected exactly 1" % (job.get('name'), sched, _sched_flags))
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
        # context-weight axis: a self-contained mechanical agentTurn skips the
        # bootstrap root files (AGENTS/SOUL/IDENTITY/USER/TOOLS) it never reads,
        # re-paid on every fire otherwise. Only meaningful for message jobs.
        if pay.get('lightContext'):
            a += ['--light-context']
    _ta = pay.get('toolsAllow') or job.get('toolsAllow')
    if _ta:
        a += ['--tools', ','.join(_ta) if isinstance(_ta, (list, tuple)) else str(_ta)]
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
        lr = subprocess.run(['openclaw', 'cron', 'list', '--all', '--json'], capture_output=True, text=True, timeout=10)
        if lr.returncode != 0:
            return ''
        data = json.loads(lr.stdout)
        joblist = data if isinstance(data, list) else (data.get('jobs') if isinstance(data.get('jobs'), list) else (data.get('jobs') or {}).get('jobs', []))
        for j in (joblist or []):
            if _dino_name_agent_match(j, name):
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
        lr = subprocess.run(['openclaw', 'cron', 'list', '--all', '--json'], capture_output=True, text=True, timeout=10)
        if lr.returncode == 0:
            data = json.loads(lr.stdout)
            joblist = data if isinstance(data, list) else (data.get('jobs') if isinstance(data.get('jobs'), list) else (data.get('jobs') or {}).get('jobs', []))
            for j in (joblist or []):
                if _dino_name_agent_match(j, name):
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

import os

def _find_cron(name):
    try:
        lr = subprocess.run(['openclaw', 'cron', 'list', '--all', '--json'], capture_output=True, text=True, timeout=10)
        if lr.returncode != 0:
            return ''
        data = json.loads(lr.stdout)
        joblist = data if isinstance(data, list) else (data.get('jobs') if isinstance(data.get('jobs'), list) else (data.get('jobs') or {}).get('jobs', []))
        for j in (joblist or []):
            if _dino_name_agent_match(j, name):
                return j.get('id','')
    except Exception:
        return ''
    return ''

def upsert_gated_worker(job, label):
    """Register a gate-driven agentTurn worker: created/kept DISABLED (never
    self-fires), returns its job id (or '' on failure). Upgrade-safe: if it exists,
    refresh the prompt AND force it disabled (an older install may have left it
    self-scheduled). The gate wiring + fallback is handled by the caller."""
    name = job['name']
    existing_id = _find_cron(name)
    jid = ''
    try:
        if existing_id:
            jid = existing_id
            msg = job.get('payload', {}).get('message', '')
            subprocess.run(['openclaw', 'cron', 'edit', existing_id, '--message', msg],
                           capture_output=True, text=True, timeout=15)
            print(f"  \033[32m[ok]\033[0m   {label} OpenClaw cron updated (prompt refreshed)")
        else:
            j2 = dict(job); j2['enabled'] = False
            ar = subprocess.run(_cron_add_argv(j2), capture_output=True, text=True, timeout=15)
            if ar.returncode != 0:
                print(f"  \033[33m[warn]\033[0m Could not register {label} cron: {ar.stderr[:120]}")
                return ''
            try:
                jid = json.loads(ar.stdout).get('id','')
            except Exception:
                jid = _cron_verify(name)
            if not jid:
                print(f"  \033[31m[FAIL]\033[0m  {label} cron did NOT persist. Add it manually. Install continues.")
                return ''
            print(f"  \033[32m[ok]\033[0m   {label} OpenClaw cron registered (disabled) \u2713 verified")
    except Exception as e:
        print(f"  \033[33m[warn]\033[0m {label} upsert failed: {e}")
        return ''
    return jid

# Daily Note Review is verification + rubric-driven GC (run locally-checkable
# done_when conditions, flip/delete on the result) — not deep reasoning. Pin it
# to the cheap/non-reasoning model when DINOMEM_CHEAP_MODEL is set; unset ->
# agent default (default-safe). Matches neuron's equivalent job.
_cheap = os.environ.get("DINOMEM_CHEAP_MODEL", "").strip()
job = {
    "name": "Daily Note Review",
    "schedule": {"kind": "cron", "expr": "0 6 * * *", "tz": "UTC"},
    "payload": {
        "kind": "agentTurn",
        **({"model": _cheap} if _cheap else {}),
        "message": "SCOPE LOCK (READ FIRST, NON-NEGOTIABLE): You operate ONLY on files whose basename matches the glob _note_*.md in $WS/memory/. Get the exact set by running: ls $WS/memory/_note_*.md. If that returns nothing / no matches, STOP IMMEDIATELY, touch nothing, output exactly NO_REPLY. You must NEVER read, evaluate, flip, delete, or GC any file that is not a _note_*.md file. In particular you must NEVER touch: _pin_*.md, _permanent*.md, MEMORY.md, or any date-prefixed distilled memory file (e.g. 2026-06-20_insight_*.md, *_entity_*.md, *_preference_*.md, *_relation_*.md, *_insight_*.md). Those are PERMANENT KNOWLEDGE, not task notes — deleting one is data loss. Do NOT glob memory/*.md, do NOT 'scan all memory files', do NOT infer that a distilled memory file is a resolvable note. If the count of _note_*.md files you are about to process is more than a handful (say >50) or includes anything not literally named _note_*.md, that is a BUG in your file selection — STOP and report it instead of acting. Only after this scope lock is satisfied, proceed.\n\nScan the _note_*.md files in $WS/memory/. Resolve each note (today = current UTC date): 1) task_bound notes (have done_when:): verify the done_when condition against workspace state (file exists, feature shipped). If verified, flip status to done and delete the note (promote to _pin_*.md if it has lasting value). Else leave pending. 2) type:project notes (project executor schema, may be added by neuron): these are normally advanced/closed by the neuron Project Advancer (base does not run it), BUT a project can be finished out-of-band by a human-driven session and left status:in_progress, or parked at a safety-gated final step (git push / external action) that the Advancer is forbidden to run — so it would otherwise orphan here. For any type:project note, verify its done_when the SAME way as task_bound (run the locally-checkable condition; e.g. for a git-push done_when run the rev-parse HEAD==@{u} check). If done_when verifies (and/or all steps are [x]), flip status to done and delete/promote it. If it is in_progress and clearly still has unchecked non-gated steps, leave it for the neuron Advancer (if installed). Do not delete a project whose done_when does not verify. 3) type:brainstorm notes (settled-thinking, status:design; a note class the neuron install may add — NOT tasks, so they carry NO done_when): a brainstorm can have a SHIPPABLE outcome that already landed out-of-band in a live session, leaving it stranded at status:design. IF such a note carries a shipped_when: field, verify it the SAME locally-checkable way as done_when (file exists / grep / exit 0 / compare the repo HEAD commit to its upstream @{u} via git rev-parse run in the repo dir and confirm HEAD equals upstream). If shipped_when verifies, flip status:design -> status:resolved and LEAVE THE NOTE IN PLACE (never delete a brainstorm — its thinking is the value; resolved brainstorms are retained/promoted, not reaped). If shipped_when does not verify, or the brainstorm has NO shipped_when field, leave it untouched (pure open-ended thinking has no machine-checkable resolution and stays human-resolved). Never delete a type:brainstorm note here. 4) stale_after GC: if a note is still pending/in_progress AND done_when was never met AND today > stale_after (default date+30d, or date+7d for reminder/quick-todo notes), delete it as abandoned. 5) Legacy notes with no schema fields: infer the task from content, delete if clearly resolved, else leave. Leave untouched any fields you do not recognize. Report what resolved, what was GC'd, and what remains.",
        "lightContext": True,
        "timeoutSeconds": 300
    },
    "sessionTarget": "isolated",
    "delivery": {"mode": "none"}
}
DNR_ID = upsert_gated_worker(job, "note_review")

# Pending Note Reminder — same cheap-model rationale. delivery: announce (it
# messages the user with the reminder). Gated too: check_pending_notes.py is the
# zero-LLM prefilter, run by the gate BEFORE any LLM turn is spent.
job = {
    "name": "Pending Note Reminder",
    "schedule": {"kind": "cron", "expr": "0 9 */3 * *"},
    "payload": {
        "kind": "agentTurn",
        **({"model": _cheap} if _cheap else {}),
        "message": "Run: python3 $WS/scripts/check_pending_notes.py\n\nIf exit code is 1 (no output) -> NO_REPLY, stop here, zero LLM cost.\n\nIf exit code is 0 (JSON output) -> for each note in the JSON:\n1. Read the full note file\n2. Evaluate done_when — run any shell command if verifiable, or reason from context\n3. If done -> update status to done in the file, report which ones closed\n4. If not done -> include in reminder summary to user\n\nSend reminder only if there are notes still pending after evaluation. Format: brief list with note title + stale_after date.",
        "lightContext": True,
        "timeoutSeconds": 600
    },
    "sessionTarget": "isolated",
    "delivery": {"mode": "announce"}
}
PNR_ID = upsert_gated_worker(job, "pending_note_reminder")

# ── SAME-NAME DEDUP SWEEP (idempotency guarantee across ANY prior state) ─────
# The upsert helpers already update-by-name so a clean re-run never duplicates,
# BUT a PRIOR half-finished/crashed run (or a manual add) could have left two jobs
# sharing a canonical name; the by-name lookup then picks one arbitrarily and the
# other keeps firing as a zombie. Sweep every canonical base lane: if >1 job shares
# it, keep the NEWEST (highest createdAtMs) and remove the rest. Makes 'update
# dinomem' converge to exactly one of each lane from any starting state. Pure
# cleanup: never creates anything, only removes true dupes.
_CANON_BASE = ["Daily Note Review", "Pending Note Reminder", "Note Cron Gate"]
try:
    _lr = subprocess.run(['openclaw','cron','list','--json'], capture_output=True, text=True, timeout=10)
    _data = json.loads(_lr.stdout) if _lr.returncode == 0 else []
    _jl = _data if isinstance(_data, list) else _data.get('jobs', {}).get('jobs', _data.get('jobs', []))
except Exception:
    _jl = []
for _cn in _CANON_BASE:
    _m = [j for j in (_jl or []) if _dino_name_agent_match(j, _cn)]
    if len(_m) <= 1:
        continue
    _m.sort(key=lambda j: (j.get('createdAtMs', 0), j.get('updatedAtMs', 0)), reverse=True)
    for _dup in _m[1:]:
        _did = _dup.get('id','')
        if _did:
            subprocess.run(['openclaw','cron','remove',_did], capture_output=True, text=True, timeout=15)
            print("  \033[32m[ok]\033[0m   deduped '%s' — removed duplicate %s (kept newest)" % (_cn, _did[:8]))

# ── Note Cron Gate (command cron, */15, ZERO LLM) ───────────────────────────
# One canonical gate. If it already exists (e.g. a prior run, or neuron created
# it), MERGE our two lane ids into its existing env (never clobber neuron's trio
# ids). --command-env REPLACES the whole env set, so we always re-send the full
# merged dict. If the gate cannot be created (command crons refused), fall back
# to re-enabling both workers on their own schedules.
WS = os.environ.get("OPENCLAW_WORKSPACE") or os.environ.get("DINOMEM_WS", "")
GATE_NAME = "Note Cron Gate"

def _gate_env(gate_id):
    try:
        g = subprocess.run(['openclaw', 'cron', 'get', gate_id, '--json'], capture_output=True, text=True, timeout=15)
        if g.returncode == 0:
            return (json.loads(g.stdout).get('payload', {}) or {}).get('env', {}) or {}
    except Exception:
        pass
    return {}

def _fallback_selfsched():
    print("  \033[33m[WARN]\033[0m Note Cron Gate could not be installed (command crons refused?).")
    print("  \033[33m[WARN]\033[0m Falling back: re-enabling Daily Note Review + Pending Note Reminder on their own schedules.")
    print("  \033[33m[WARN]\033[0m That path COSTS an LLM turn on every idle fire. Allow command crons (operator.admin) + re-run for the free path.")
    for jid in (DNR_ID, PNR_ID):
        if jid:
            subprocess.run(['openclaw', 'cron', 'enable', jid], capture_output=True, text=True, timeout=15)

if not (DNR_ID or PNR_ID):
    print("  \033[33m[warn]\033[0m No worker ids captured — skipping Note Cron Gate wiring.")
else:
    lane_env = {}
    if DNR_ID: lane_env["GATE_DAILY_NOTE_REVIEW_ID"] = DNR_ID
    if PNR_ID: lane_env["GATE_PENDING_REMINDER_ID"] = PNR_ID
    gate_id = _find_cron(GATE_NAME)
    if gate_id:
        # Gate exists — MERGE our lanes into its env (preserve any neuron lanes).
        env = _gate_env(gate_id); env["OPENCLAW_WORKSPACE"] = env.get("OPENCLAW_WORKSPACE", WS); env.update(lane_env)
        args = ['openclaw', 'cron', 'edit', gate_id]
        for k, v in env.items():
            args += ['--command-env', f'{k}={v}']
        subprocess.run(args, capture_output=True, text=True, timeout=15)
        subprocess.run(['openclaw', 'cron', 'enable', gate_id], capture_output=True, text=True, timeout=15)
        print(f"  \033[32m[ok]\033[0m   Note Cron Gate found — merged base lanes into its env (gate-driven, zero idle LLM)")
    else:
        gate = {
            "name": GATE_NAME,
            "description": "Zero-LLM dispatcher: runs cheap note checks every 15min, force-runs a worker only when it reports work.",
            "schedule": {"kind": "cron", "expr": "*/15 * * * *", "tz": "UTC"},
            "payload": {
                "kind": "command",
                "argv": ["sh", "-lc", "bash " + WS + "/scripts/cron_gate.sh"],
                "cwd": WS,
                "env": {"OPENCLAW_WORKSPACE": WS, **lane_env},
            },
            "delivery": {"mode": "none"},
        }
        ar = subprocess.run(_cron_add_argv(gate), capture_output=True, text=True, timeout=15)
        gid = ''
        try:
            gid = json.loads(ar.stdout).get('id','')
        except Exception:
            gid = _cron_verify(GATE_NAME)
        # Confirm the gateway stored a REAL command-kind job (some builds downgrade).
        kind_ok = False
        if gid:
            try:
                g = subprocess.run(['openclaw', 'cron', 'get', gid, '--json'], capture_output=True, text=True, timeout=15)
                if g.returncode == 0:
                    kind_ok = (json.loads(g.stdout).get('payload', {}) or {}).get('kind') == 'command'
            except Exception:
                kind_ok = False
        if gid and kind_ok:
            print(f"  \033[32m[ok]\033[0m   Note Cron Gate registered (*/15 command cron, zero-LLM) — drives note janitor lanes")
        else:
            if gid:
                subprocess.run(['openclaw', 'cron', 'remove', gid], capture_output=True, text=True, timeout=15)
            _fallback_selfsched()
PYEOF
  fi

  # ── Log rotation for the cron logs dinomem writes ───────────────────────────
  # Every dinomem cron appends to logs/*.log with NO rotation. On a busy box the
  # */15 auto_session_reset + extraction logs grow unbounded (observed: 175MB+
  # auto_reset.log, 161MB session_reset.log) until they threaten disk. We drop a
  # logrotate config generated from $WS. Prefer the system dir (/etc/logrotate.d)
  # when writable; otherwise write a workspace-local config the operator can wire
  # into their own logrotate (documented in the warning). copytruncate is used so
  # append-mode crons keep writing without a restart.
  if [ "$DRY_RUN" = 1 ]; then
    plan "install logrotate config for $WS/logs/*.log (size 10M, keep 3, compress, copytruncate)"
  else
    LOGROTATE_BODY="$WS/logs/*.log {
    size 10M
    rotate 3
    missingok
    notifempty
    compress
    delaycompress
    copytruncate
}"
    if [ -d /etc/logrotate.d ] && [ -w /etc/logrotate.d ]; then
      LR_DST="/etc/logrotate.d/dinomem-$AGENT_ID"
      printf '%s\n' "$LOGROTATE_BODY" > "$LR_DST" 2>/dev/null \
        && ok "logrotate installed: $LR_DST (10M x3, copytruncate)" \
        || warn "could not write $LR_DST — logs/*.log will not auto-rotate"
    else
      LR_DST="$WS/logs/logrotate.conf"
      mkdir -p "$WS/logs" 2>/dev/null || true
      printf '%s\n' "$LOGROTATE_BODY" > "$LR_DST" 2>/dev/null || true
      warn "no writable /etc/logrotate.d — wrote $LR_DST instead."
      warn "  Wire it in yourself, e.g. add to root crontab:"
      warn "  0 3 * * * /usr/sbin/logrotate --state $WS/logs/.logrotate.state $LR_DST"
    fi
  fi

  # TEI @reboot
  # pending_note_reminder — every 3 days via OpenClaw cron (zero LLM pre-filter + LLM evaluate)
  if [ "$DRY_RUN" = 1 ]; then
    plan "register/refresh OpenClaw cron: Pending Note Reminder (every 3 days 9:00 local)"
  else
    DINOMEM_AGENT_ID="$AGENT_ID" python3 - <<'PYEOF'
import subprocess, json
import os as _os
_DINO_AID = (_os.environ.get('DINOMEM_AGENT_ID','') or '').strip().lower()
def _dino_name_agent_match(j, name):
    if j.get('name','').strip().lower() != name.strip().lower():
        return False
    jaid = (j.get('agentId') or '')
    jaid = jaid.strip().lower() if isinstance(jaid, str) else ''
    # single-agent / legacy fallback: if we don't know our agent id, or the
    # stored job has no agentId, match by name only (preserves buyer path).
    if not _DINO_AID or not jaid:
        return True
    return jaid == _DINO_AID
# --dino-agent-match-helper--

def _cron_add_argv(job):
    """Build a flag-based `openclaw cron add` argv from a job dict.
    OpenClaw 2026.6.6+ has no `cron add --json <blob>`; jobs are built from flags.
    `--json` here is OUTPUT-only (so we can parse the created job id)."""
    a = ['openclaw', 'cron', 'add']
    name = job.get('name')
    if name:
        a += ['--name', name]
    _aid = (_os.environ.get('DINOMEM_AGENT_ID','') or '').strip()
    if _aid:
        a += ['--agent', _aid]  # fix#8: agent-scope every registered job
    sched = job.get('schedule', {}) or {}
    _sched_flags = 0
    if sched.get('kind') == 'cron' and sched.get('expr'):
        a += ['--cron', sched['expr']]; _sched_flags += 1
        if sched.get('tz'):
            a += ['--tz', sched['tz']]
    elif sched.get('kind') == 'every' and sched.get('every'):
        a += ['--every', str(sched['every'])]; _sched_flags += 1
    elif sched.get('kind') == 'at' and sched.get('at'):
        a += ['--at', str(sched['at'])]; _sched_flags += 1
    # DEFENSIVE: `openclaw cron add` rejects zero-or-multiple schedule flags with
    # a cryptic 'Choose exactly one schedule' error that aborts the whole install
    # heredoc, killing every lane after it. Fail LOUD with the job name instead.
    if _sched_flags != 1:
        raise ValueError("cron job %r has a malformed schedule (%r) -> would emit %d schedule flags; expected exactly 1" % (job.get('name'), sched, _sched_flags))
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
        # context-weight axis: a self-contained mechanical agentTurn skips the
        # bootstrap root files (AGENTS/SOUL/IDENTITY/USER/TOOLS) it never reads,
        # re-paid on every fire otherwise. Only meaningful for message jobs.
        if pay.get('lightContext'):
            a += ['--light-context']
    _ta = pay.get('toolsAllow') or job.get('toolsAllow')
    if _ta:
        a += ['--tools', ','.join(_ta) if isinstance(_ta, (list, tuple)) else str(_ta)]
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
        lr = subprocess.run(['openclaw', 'cron', 'list', '--all', '--json'], capture_output=True, text=True, timeout=10)
        if lr.returncode != 0:
            return ''
        data = json.loads(lr.stdout)
        joblist = data if isinstance(data, list) else (data.get('jobs') if isinstance(data.get('jobs'), list) else (data.get('jobs') or {}).get('jobs', []))
        for j in (joblist or []):
            if _dino_name_agent_match(j, name):
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
        lr = subprocess.run(['openclaw', 'cron', 'list', '--all', '--json'], capture_output=True, text=True, timeout=10)
        if lr.returncode == 0:
            data = json.loads(lr.stdout)
            joblist = data if isinstance(data, list) else (data.get('jobs') if isinstance(data.get('jobs'), list) else (data.get('jobs') or {}).get('jobs', []))
            for j in (joblist or []):
                if _dino_name_agent_match(j, name):
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

# Same rationale as Daily Note Review: light verification/reminder work, not
# deep reasoning. Pin to the cheap/non-reasoning model when DINOMEM_CHEAP_MODEL
# is set; unset -> agent default (default-safe).
_cheap = _os.environ.get("DINOMEM_CHEAP_MODEL", "").strip()  # fix#8: block imports 'os as _os' only; bare os here -> NameError
job = {
    "name": "Pending Note Reminder",
    "schedule": {"kind": "cron", "expr": "0 9 */3 * *"},
    "payload": {
        "kind": "agentTurn",
        **({"model": _cheap} if _cheap else {}),
        "message": "Run: python3 $WS/scripts/check_pending_notes.py\n\nIf exit code is 1 (no output) -> NO_REPLY, stop here, zero LLM cost.\n\nIf exit code is 0 (JSON output) -> for each note in the JSON:\n1. Read the full note file\n2. Evaluate done_when — run any shell command if verifiable, or reason from context\n3. If done -> update status to done in the file, report which ones closed\n4. If not done -> include in reminder summary to user\n\nSend reminder only if there are notes still pending after evaluation. Format: brief list with note title + stale_after date.",
        "lightContext": True,
        "timeoutSeconds": 600
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

# ── 4b) Cheap-model advisory (non-blocking, human-must-see) ───────────────────
# dinomem's non-reasoning LLM lanes (extract + review) now AUTO-FOLLOW your
# agents.defaults.compaction.model. If that anchor is unset, those lanes fall
# back to your (expensive) default model on EVERY 15-min extract + daily review.
# We do NOT block the install (noob-seamless) — but the human MUST see this, and
# if an AI agent is running the installer, it must RELAY this to its human.
#
# Resolve what the cheap tier currently resolves to (empty => unset => default).
cheap_now=""
if command -v python3 >/dev/null 2>&1 && [ -f "$SKILL_DIR/procedures/_cheap_model.py" ]; then
  cheap_now="$(OPENCLAW_CONFIG="${OPENCLAW_JSON:-$HOME/.openclaw/openclaw.json}" python3 "$SKILL_DIR/procedures/_cheap_model.py" 2>/dev/null || true)"
fi
if [ -z "$cheap_now" ]; then
  # Marker file: an AI agent running this installer is instructed (README +
  # this marker) to read it and DELIVER the advisory to its human operator.
  CHEAP_WARN_MARKER="${TMPDIR:-/tmp}/dinomem_cheap_model_advisory.txt"
  {
    echo "DINOMEM COST ADVISORY — no cheap model set"
    echo ""
    echo "dinomem's non-reasoning work (memory extraction every 15 min + daily"
    echo "review) has NO cheap model configured, so it will run on your DEFAULT"
    echo "(reasoning) model. That is the most expensive way to run bulk memory work."
    echo ""
    echo "Fix (recommended): set a cheap high-context model as the anchor —"
    echo "  openclaw config set agents.defaults.compaction.model <cheap-model-id>"
    echo "Everything (extraction, review, and any cheap-tagged crons) auto-follows it."
    echo ""
    echo "Nothing is broken — this is a cost note, not an error. Install continued."
  } > "$CHEAP_WARN_MARKER" 2>/dev/null || true
  printf '\n\033[1;33m'
  printf '╔══════════════════════════════════════════════════════════════════╗\n'
  printf '║  ⚠  DINOMEM COST ADVISORY — READ THIS (no cheap model set)        ║\n'
  printf '╚══════════════════════════════════════════════════════════════════╝\033[0m\n'
  printf '  dinomem'\''s bulk memory work (extract every 15 min + daily review)\n'
  printf '  has NO cheap model set, so it runs on your DEFAULT (reasoning) model —\n'
  printf '  the most expensive option for high-volume, no-reasoning work.\n\n'
  printf '  \033[1mRecommended:\033[0m set a cheap high-context model as the single anchor:\n'
  printf '    \033[36mopenclaw config set agents.defaults.compaction.model <cheap-model-id>\033[0m\n'
  printf '  Extraction, review, and cheap crons all auto-follow it.\n\n'
  printf '  Nothing is broken. Install continues. (Advisory saved: %s)\n' "${CHEAP_WARN_MARKER}"
  printf '\033[1;33m  → If an AI agent ran this install: DELIVER this advisory to your human.\033[0m\n\n'
else
  ok "cheap model resolves to: $cheap_now (auto-linked to compaction.model)"
fi

# ── 5) Patch openclaw.json config ─────────────────────────────────────────────
hr "OpenClaw config"
OPENCLAW_JSON="${OPENCLAW_JSON:-$HOME/.openclaw/openclaw.json}"
[ -f "$OPENCLAW_JSON" ] || OPENCLAW_JSON="$OPENCLAW_DIR/openclaw.json"
if [ -f "$OPENCLAW_JSON" ] && [ "$DRY_RUN" != 1 ]; then
  bash "$SKILL_DIR/scripts/file-backup.sh" "$OPENCLAW_JSON" >/dev/null 2>&1 && ok "openclaw.json backed up" || true  # backup is a safety nicety; original untouched, so a failed .bak is not user-actionable — stay silent
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
        # Reliability: TEI embed blip/timeout -> in-process local embedder
        # instead of hard-failing the whole search (avoids repeat 15s timeouts).
        "fallback": "local",
        "query": {"hybrid": {"vectorWeight": 0.7, "textWeight": 0.3}},
    }
    changed.append("memorySearch -> TEI openai-compatible (localhost:8080)")
elif not mem_search.get("fallback"):
    # Existing openai-compatible install missing the reliability fallback:
    # top it up idempotently so a TEI blip degrades to the local embedder
    # instead of hard-failing memory_search.
    mem_search["fallback"] = "local"
    defaults["memorySearch"] = mem_search
    changed.append("memorySearch.fallback -> local (reliability top-up)")

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
  SC_DIR="$OPENCLAW_DIR/extensions/smart-cache-pro"
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
      warn "$SC_DIR exists but is not a git repo — backing up and re-cloning"
      mv "$SC_DIR" "$SC_DIR.bak.$(date +%s)"
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
import json, os, sys
path, sc_dir = sys.argv[1], sys.argv[2]
canonical = os.path.realpath(sc_dir)
with open(path) as f:
    cfg = json.load(f)
plugins = cfg.setdefault("plugins", {})
load = plugins.setdefault("load", {})
paths = load.get("paths")
if not isinstance(paths, list):
    paths = []
changed = []
# Keep only the canonical smart-cache-pro path. Remove stale copies that may
# have been left by earlier installs under /tmp or $OPENCLAW_DIR root so
# repeated installs/updates don't accumulate duplicate plugin IDs.
stale = [p for p in paths if os.path.basename(p) == "smart-cache-pro" and os.path.realpath(p) != canonical]
for p in stale:
    paths.remove(p); changed.append(f"removed stale smart-cache-pro path: {p}")
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

# ── 5b) git-autosnapshot feature (default-ON; --no-git-snapshot to skip) ──────
# ISOLATED git snapshot safety net for the whole ~/.openclaw repo: a timer
# commits all non-ignored changes every 15 min into a SEPARATE git-dir
# (.dinomem-snap.git), with disk-aware cleanup, lfs media handling, and history
# retention. It NEVER touches the user's own repo — separate git-dir + private
# info/exclude, no .gitignore/.gitattributes dropped into their working tree.
# Because it's fully isolated it's safe to default ON. Self-contained under features/.
if [ "$DO_GIT_SNAPSHOT" = 1 ]; then
  hr "git-autosnapshot (isolated snapshot store)"
  GS_INSTALLER="$SKILL_DIR/features/git-autosnapshot/install.sh"
  GS_REPO="$OPENCLAW_DIR"   # the ~/.openclaw root (parent of the workspace)
  if [ ! -f "$GS_INSTALLER" ]; then
    warn "features/git-autosnapshot/install.sh not found — skipping"
  elif [ "$DRY_RUN" = 1 ]; then
    plan "run git-autosnapshot installer against $GS_REPO (every 15 min, disk-aware)"
  else
    GS_ARGS=(--repo "$GS_REPO")
    [ "$FORCE" = 1 ] && GS_ARGS+=(--force)
    if bash "$GS_INSTALLER" "${GS_ARGS[@]}"; then
      ok "git-autosnapshot installed (repo: $GS_REPO)"
    else
      warn "git-autosnapshot installer returned non-zero — check output above"
    fi
  fi
else
  skip "git-autosnapshot (disabled via --no-git-snapshot)"
fi

# ── 5c) grep-guard feature (default-ON, ANNOUNCED; --no-grep-guard to skip) ───
# PATH-ahead shim at /usr/local/bin/grep that blocks ONLY broad recursive greps
# over large directory trees (size/depth heuristic) -> exit 2 with a scope-down
# hint. Every other grep passes straight through to the real binary. Because it
# sits ahead of the real grep in PATH it affects the WHOLE machine, so its own
# installer prints a LOUD banner naming what it does + how to remove it (no
# silent core-binary hijack). Safe to default ON *because* it self-announces and
# has a clean uninstall + GREPGUARD_OFF=1 kill switch. Self-contained under features/.
if [ "$DO_GREP_GUARD" = 1 ]; then
  hr "grep-guard (recursive-grep shim)"
  GG_INSTALLER="$SKILL_DIR/features/grep-guard/install.sh"
  if [ ! -f "$GG_INSTALLER" ]; then
    warn "features/grep-guard/install.sh not found — skipping"
  elif [ "$DRY_RUN" = 1 ]; then
    plan "run grep-guard installer (shim at /usr/local/bin/grep, size/depth-based, announced)"
  else
    GG_ARGS=()
    [ "$FORCE" = 1 ] && GG_ARGS+=(--force)
    if bash "$GG_INSTALLER" "${GG_ARGS[@]}"; then
      ok "grep-guard installed (broad recursive greps guarded; GREPGUARD_OFF=1 to disable once)"
    else
      warn "grep-guard installer returned non-zero — check output above (grep still works normally)"
    fi
  fi
else
  skip "grep-guard (disabled via --no-grep-guard)"
fi

# ── 6) Wire AGENTS.md ──────────────────────────────────────────────
hr "AGENTS.md"
AGENTS="$WS/AGENTS.md"
if [ -f "$AGENTS" ] && [ "$DRY_RUN" != 1 ]; then
  bash "$SKILL_DIR/scripts/file-backup.sh" "$AGENTS" >/dev/null 2>&1 && ok "AGENTS.md backed up" || true
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
    M0: context_unclear → recall_first (memory_search + memory_get; upgraded_recall_door replaces if present); fallback: ask
    M1_M2: recall_first before fs/exec/tool/ship + before any draft asserting action|property about a named/known entity — dinomem-recall-gate enforces the tool-call half; model enforces the pre-tool draft half. Also: read open notes (see open_work manifest) before building.
    M3_query_style:
      applies_to: memory_search
      prefer: natural_language
      avoid: technical_identifiers | code_terms | exact_strings | variable_names
      enforce: rewrite query to natural language before calling any memory tool

  investigate_before_act: cross_entity|stale_claim|bug — inspect real artifact before answering; re-run after fix; mutable facts need live check every turn (gate enforces cold fs/exec reach; model owns reasoning correctness)

  memory_tools:
    memory_search: simple recall — facts, preferences, decisions, context; default for most queries
  memory_recall:
    after_search: memory_get on relevant result
    skip: do not call memory_search every turn

  skills:
    memory_pin: when user says remember/pin/note this, you commit to deferred work, or a todo/reminder/time-bound/project task arises → read skill "memory-pinning" for _pin_/_note_/project format + done_when rules
    self_config: when user implies changing behavior/rules/workflow/persona/tools/preferences → read skill "self-config"
    cron_config: when user implies scheduling/reminding/recurring checks/automation timing ("every day", "remind me", "check X periodically") → read skill "cron-config"
    hook_config: when user implies reacting to a gateway event ("every time X happens", on session-start/inbound-message/command/compaction/lifecycle) → read skill "hook-config"
    skill_config: when user wants the agent to learn a repeatable method/procedure needed sometimes (not every turn) → read skill "skill-config"
    build_quality: BEFORE writing >a few lines of new self-mod code (a cron gate, hook handler, skill body, tool, lib fn) → read skill "build-quality" (reuse→DRY→minimal→doc-why→test; run route.py dup + route.py verify)
    backup_restore: when user asks to undo/restore a file or memory change, or what backups exist → read skill "backup-restore"
    safe_file_ops: BEFORE any file edit, large write (>~6KB), or multi-step exec → read skill "safe-file-ops" (read-first/exact-oldText/verify-after, atomic-batch rollback trap, incremental large-write, exec single-command discipline). Pre-edit hygiene half; edit-verify-loop is the post-edit syntax-gate half.

  durability_tripwire:
    fires_on: own_draft pre_send
    smell: draft commits remember|note|always|never_again|from_now_on X AND only THIS session context makes it stick
    rule: red_flag NOT solution; context_only_fix=gone_next_session; STOP send → self_modification_request → route.py classify → land X on durable_surface[cron|hook|skill|tool|root]
    forbid: ship "ok I'll remember X" | one_turn_change as_fix; change=SYSTEMATIC not context_dependent
    exit: skip only if X one_off no_future_relevance OR already on durable_surface[verify not_assume]

  config_write_safety: NEVER raw-edit openclaw.json → use `openclaw config patch/set` (validated; refuses bad JSON). One trailing comma = crash-loop. Gate (dinomem-recall-gate Tier B) re-arms on openclaw.json writes.

  reply_to_context:
    rule: inbound message has reply_to_id AND the referenced message is visible in the injected conversation context -> read that message FIRST before firing any tool or search
    forbid: searching/tooling for information already present in the injected context window
    enforce: reply_to_id present -> scan injected context for the referenced message id -> answer from it; only tool/search if NOT found in context
    rationale: context-reading failure (ignoring what is already visible) is not a memory gap; the rule must be explicit
  recursive_search_discipline:
    rule: recursive search -> scope to the smallest relevant dir + a timeout (e.g. `rg <pat> <small-dir>` or `timeout 20 grep -r <pat> <small-dir>`); never bare-grep the whole tree.
    forbid: bare `grep -r` / `grep -R` over huge roots (the OpenClaw home, agent session dirs, kb/, $HOME) -> slow AND floods context with noise. Scope down first.
    prefer: ripgrep (`rg`) when available (fast, respects .gitignore); fall back to a timeout-bounded scoped grep.
    override: need a raw recursive grep for a real reason -> call the real binary directly (`/usr/bin/grep`), bypassing any installed grep-guard.
    note: the optional grep-guard feature (features/grep-guard) enforces this at the shell level when installed; this rule holds regardless.
DINOMEM_AGENTS_BODY
)
BLOCK="$BEGIN
$DINOMEM_BODY
$END"

# Legacy-absorb pre-pass: if an OLD unmarked dinomem section exists (pre-marker
# installs) AND no BEGIN marker yet, strip it so wire_managed_block doesn't leave
# a duplicate unmarked copy above the fresh managed block. Marker-bounded blocks
# are never touched here (this only runs when no BEGIN marker exists).
if [ "$DRY_RUN" != 1 ] && ! grep -qF "$BEGIN" "$AGENTS" 2>/dev/null \
   && grep -qE '^## (dinomem|memory_recall|rag_long_docs)([[:space:]]|$)' "$AGENTS" 2>/dev/null; then
  _tmp_legacy="$(mktemp)"
  awk '
    /^## (dinomem|memory_recall|rag_long_docs)([ \t]|$)/ { drop=1; next }
    drop && /^#{1,2} / { drop=0 }
    !drop { print }
  ' "$AGENTS" > "$_tmp_legacy"
  awk 'NF{last=NR} {lines[NR]=$0} END{for(i=1;i<=last;i++) print lines[i]}' "$_tmp_legacy" > "$AGENTS"
  rm -f "$_tmp_legacy"
fi
wire_managed_block "$AGENTS" "$BEGIN" "$END" "$BLOCK" "AGENTS.md"
if false; then  # DEAD: legacy wiring, superseded by wire_managed_block above; kept guarded then removed
  if [ "$FORCE" = 1 ]; then
    if [ "$DRY_RUN" = 1 ]; then
      plan "refresh dinomem managed block in AGENTS.md (strip old BEGIN..END, write current)"
    else
      _tmp_agents="$(mktemp)"
      awk -v b="$BEGIN" -v e="$END" '
        index($0,b){skip=1}
        !skip{print}
        index($0,e){skip=0}
      ' "$AGENTS" > "$_tmp_agents"
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
  # No modern marker present. A PRE-MARKER install may still have left an
  # UNMARKED legacy dinomem section (## dinomem / ## memory_recall /
  # ## rag_long_docs) written directly into AGENTS.md. Appending the fresh
  # marked block without removing it leaves a stale duplicate (the old
  # warn-only path). Absorb it: strip any such unmarked top-level section
  # (from its '## <header>' line up to the next '## '/'# ' header or EOF),
  # then append the current marked block. Marker-bounded blocks are never
  # touched here (this branch only runs when no BEGIN marker exists).
  if grep -qE '^## (dinomem|memory_recall|rag_long_docs)([[:space:]]|$)' "$AGENTS" 2>/dev/null; then
    _tmp_legacy="$(mktemp)"
    awk '
      /^## (dinomem|memory_recall|rag_long_docs)([ \t]|$)/ { drop=1; next }
      drop && /^#{1,2} / { drop=0 }
      !drop { print }
    ' "$AGENTS" > "$_tmp_legacy"
    # Trim trailing blank lines the removal may leave.
    awk 'NF{last=NR} {lines[NR]=$0} END{for(i=1;i<=last;i++) print lines[i]}' "$_tmp_legacy" > "$AGENTS"
    rm -f "$_tmp_legacy"
    printf '\n%s\n' "$BLOCK" >> "$AGENTS"
    ok "AGENTS.md wired (absorbed unmarked legacy dinomem section into managed block)"
  else
    printf '\n%s\n' "$BLOCK" >> "$AGENTS"
    ok "AGENTS.md wired"
  fi
fi

# ── 6b) Wire TOOLS.md ────────────────────────────────────────────────────────
hr "TOOLS.md"
TOOLS="$WS/TOOLS.md"
TOOLS_MARKER="# dinomem: workspace_backup"
TOOLS_END_MARKER="# END dinomem: workspace_backup"
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
$TOOLS_BODY
$TOOLS_END_MARKER"

wire_managed_block "$TOOLS" "$TOOLS_MARKER" "$TOOLS_END_MARKER" "$TOOLS_BLOCK" "TOOLS.md"

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

# ── Cron self-check + REQUIRED-CRON GATE with auto-repair ────────────────────
# The note lifecycle only self-drives if base's cron lanes actually landed. The
# registration path is best-effort (a failed `cron add` just warns + continues,
# exit 0), so a silent gap used to run forever unnoticed. Now: verify the REQUIRED
# lanes, and if any is missing while the gateway is reachable, AUTO-REPAIR via a
# cron-only re-run (--repair-cron, idempotent). If it still can't register them,
# surface ONE copy-paste fix command and (on a normal install) exit NONZERO so the
# gap is loud. Required = Note Cron Gate + Daily Note Review (the janitor spine).
# Pending Note Reminder is recommended-not-required (a reminder, not the lifecycle).
REQUIRED_CRON_GAP=0
if openclaw_running; then
  hr "Cron self-check"
  _CRON_LIST="$(openclaw cron list --all --json 2>/dev/null || echo '[]')"
  _MISSING_REQUIRED=""
  for _cname in "Note Cron Gate" "Daily Note Review"; do
    if grep -qiF "$_cname" <<< "$_CRON_LIST"; then
      ok "cron present: $_cname"
    else
      warn "cron MISSING (required): $_cname"
      _MISSING_REQUIRED="${_MISSING_REQUIRED}${_MISSING_REQUIRED:+, }$_cname"
    fi
  done
  if grep -qiF "Pending Note Reminder" <<< "$_CRON_LIST"; then
    ok "cron present: Pending Note Reminder"
  else
    warn "cron MISSING (recommended): Pending Note Reminder — reminder lane; not blocking"
  fi
  if [ -n "$_MISSING_REQUIRED" ]; then
    _REPAIR_CMD="bash $SKILL_DIR/scripts/install.sh --workspace $WS --agent-id $AGENT_ID --repair-cron"
    # Only auto-repair on a NORMAL install; if we are already inside --repair-cron
    # and lanes are STILL missing, a re-run won't help (real gateway/permission
    # problem) — don't loop, just surface the manual command + fail.
    if [ "$REPAIR_CRON" = 0 ]; then
      printf '  \033[33m[repair]\033[0m required cron(s) missing (%s) — auto-repairing (cron-only re-run)...\n' "$_MISSING_REQUIRED"
      if bash "$SKILL_DIR/scripts/install.sh" --workspace "$WS" --agent-id "$AGENT_ID" --repair-cron; then
        _CRON_LIST2="$(openclaw cron list --all --json 2>/dev/null || echo '[]')"
        _STILL_MISSING=""
        for _cname in "Note Cron Gate" "Daily Note Review"; do
          grep -qiF "$_cname" <<< "$_CRON_LIST2" || _STILL_MISSING="${_STILL_MISSING}${_STILL_MISSING:+, }$_cname"
        done
        if [ -z "$_STILL_MISSING" ]; then
          ok "auto-repair fixed all required crons"
          _MISSING_REQUIRED=""
        else
          warn "auto-repair ran but these are STILL missing: $_STILL_MISSING"
          _MISSING_REQUIRED="$_STILL_MISSING"
        fi
      else
        warn "auto-repair (--repair-cron) exited nonzero — see output above"
      fi
    fi
    if [ -n "$_MISSING_REQUIRED" ]; then
      warn "required cron(s) still missing: $_MISSING_REQUIRED"
      printf '  \033[33m[fix]\033[0m Run this to register them (idempotent, cron-only):\n'
      printf '        %s\n' "$_REPAIR_CMD"
      printf '  \033[33m[fix]\033[0m If it keeps failing: your gateway may reject command-kind crons.\n'
      printf '        Allow command cron jobs (operator.admin) on the gateway, then re-run the above.\n'
      REQUIRED_CRON_GAP=1
    fi
  fi
fi

# ── Hook liveness self-check (L3/L3b) ───────────────────────────────────────
# `openclaw hooks enable` only sets the config flag; it does NOT guarantee the
# gateway can actually LOAD the hook. If $WS is not the gateway's scanned
# workspace, the hook lands in an unscanned $WS/hooks/ and silently never fires.
# Assert eligibility via `hooks check --json` (grep on the human table is
# unreliable: emoji wrap splits names). On failure, fall back to installing the
# hook pack into the always-scanned global ~/.openclaw/hooks/<name>/ and re-enable.
if openclaw_running; then
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

# ── Install dinomem-recall-gate plugin (before_tool_call mid-session recall) ──
# The bootstrap dinomem-open-notes hook injects the recall gate ONCE at agent
# bootstrap; it CANNOT cover mid-session (turn 5, turn 20...). This plugin closes
# that gap: a before_tool_call hook that fires on the DANGER, not the message —
# if the model reaches for a COLD fs/exec tool (exec/read/grep/glob) without
# having run any recall tool yet this turn, it gets ONE block telling it to recall
# first. LANGUAGE-AGNOSTIC by construction (no message parsing). recallTools/fsTools
# are config-driven; BASE ships the native retrieval set (memory_search/memory_get).
# Fail-open: never bricks the tool loop. neuron OVERWRITES only this plugin's
# openclaw.plugin.json with its neuron-tier recallTools — same base-owns / neuron-
# extends pattern as extract_memory.py. Installs to the PERSISTENT global extensions
# dir (plugin registration in openclaw.json is global; a workspace-relative path
# dangles and crash-loops the gateway if the workspace moves).
hr "Recall-gate hook plugin"
PLUGIN_SRC="$SKILL_DIR/plugins/dinomem-recall-gate"
PLUGIN_DST="$OPENCLAW_DIR/extensions/dinomem-recall-gate"
if [ ! -d "$PLUGIN_SRC" ]; then
  warn "plugin source $PLUGIN_SRC missing — skipping (mid-session recall stays bootstrap-only)"
else
  mkdir -p "$PLUGIN_DST"
  cp "$PLUGIN_SRC/openclaw.plugin.json" "$PLUGIN_SRC/package.json" "$PLUGIN_SRC/index.ts" "$PLUGIN_DST/"
  ok "plugin files -> $PLUGIN_DST"
  if [ ! -f "$OPENCLAW_JSON" ]; then
    warn "openclaw.json not found — plugin copied but NOT wired. Add id 'dinomem-recall-gate' to plugins.allow + its path to plugins.load.paths, then restart."
  else
    DINOMEM_DRY_RUN="$DRY_RUN" DINOMEM_OPENCLAW_JSON="$OPENCLAW_JSON" DINOMEM_PLUGIN_DST="$PLUGIN_DST" DINOMEM_WS="$WS" python3 - <<'PYEOF'
import json, os

path = os.environ["DINOMEM_OPENCLAW_JSON"]
plugin_dst = os.environ["DINOMEM_PLUGIN_DST"]
ws = os.environ["DINOMEM_WS"]
PID = "dinomem-recall-gate"
with open(path) as f:
    cfg = json.load(f)

changed = []
plugins = cfg.setdefault("plugins", {})

# 1) allowlist (create if absent so the plugin is explicitly permitted)
allow = plugins.get("allow")
if not isinstance(allow, list):
    allow = []
    plugins["allow"] = allow
if PID not in allow:
    allow.append(PID)
    changed.append(f"plugins.allow += {PID}")

# 1b) bundledDiscovery — REQUIRED companion to plugins.allow on OpenClaw 2026.6.x+.
if plugins.get("bundledDiscovery") not in ("compat", "allowlist"):
    plugins["bundledDiscovery"] = "compat"
    changed.append('plugins.bundledDiscovery -> "compat"')

# 2) load.paths -> abs plugin dir (dedup) + PRUNE stale workspace-relative paths.
load = plugins.setdefault("load", {})
paths = load.get("paths")
if not isinstance(paths, list):
    paths = []
    load["paths"] = paths
stale_paths = [p for p in paths
               if isinstance(p, str)
               and p.rstrip("/").endswith("/" + PID)
               and p != plugin_dst]
for sp in stale_paths:
    paths.remove(sp)
    changed.append(f"plugins.load.paths -= {sp} (stale)")
if plugin_dst not in paths:
    paths.append(plugin_dst)
    changed.append(f"plugins.load.paths += {plugin_dst}")

# 3) enable -> plugins.entries.<PID>.enabled. agentFilter left as the plugin's
#    shipped default ("" = all agents) so base gets mid-session recall out of the box.
entries = plugins.setdefault("entries", {})
if not isinstance(entries, dict):
    entries = {}
    plugins["entries"] = entries
entry = entries.get(PID)
if not isinstance(entry, dict):
    entry = {}
    entries[PID] = entry
if entry.get("enabled") is not True:
    entry["enabled"] = True
    changed.append(f"plugins.entries.{PID}.enabled -> true")

if changed and os.environ.get("DINOMEM_DRY_RUN") == "1":
    for c in changed:
        print(f"  \033[36m[plan]\033[0m wire openclaw.json: {c}")
elif not changed:
    print("  \033[33m[skip]\033[0m dinomem-recall-gate already wired in openclaw.json")
else:
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)
    for c in changed:
        print(f"  \033[32m[ok]\033[0m   wired: {c}")
    print("  \033[33m[warn]\033[0m Restart OpenClaw to load the plugin: openclaw gateway restart")
PYEOF
  fi
fi

# ── System tuning: reduce swap thrashing ──────────────────────────────────────
hr "System tuning"
if [ "$(uname)" = "Linux" ]; then
  # Ensure at least 4 GiB swap. WHY: dinomem's embedding/ingest path (torch +
  # sentence-transformers loading a model) spikes RAM 1.5-2.5 GiB for a few
  # seconds; on an 11 GiB box already ~6.5 GiB used, that spike + a 1 GiB swap
  # that's already full = OOM-kill. Swap is a DISK-backed safety net for the
  # transient spike, not a RAM substitute. Idempotent: skips if swap>=4 GiB
  # already; graceful: any failure warns and continues (never aborts install).
  _SWAP_MIN_MB=4096
  if [ "$(id -u 2>/dev/null || echo 1)" = "0" ]; then
    _swap_mb=$(awk '/^SwapTotal:/ {printf "%d", $2/1024}' /proc/meminfo 2>/dev/null || echo 0)
    if [ "${_swap_mb:-0}" -lt "$_SWAP_MIN_MB" ]; then
      _swapfile=/swapfile
      # Need headroom = target size + 1 GiB slack on the filesystem holding /.
      _disk_free_mb=$(df -Pm / 2>/dev/null | awk 'NR==2{print $4}')
      if [ "${_disk_free_mb:-0}" -ge $(( _SWAP_MIN_MB + 1024 )) ]; then
        # Turn off + remove any existing (smaller) /swapfile before resizing.
        [ -f "$_swapfile" ] && swapoff "$_swapfile" 2>/dev/null
        rm -f "$_swapfile" 2>/dev/null
        if (fallocate -l "${_SWAP_MIN_MB}M" "$_swapfile" 2>/dev/null \
              || dd if=/dev/zero of="$_swapfile" bs=1M count="$_SWAP_MIN_MB" 2>/dev/null) \
            && chmod 600 "$_swapfile" 2>/dev/null \
            && mkswap "$_swapfile" >/dev/null 2>&1 \
            && swapon "$_swapfile" 2>/dev/null; then
          ok "swap raised to 4 GiB (was ${_swap_mb} MiB) — OOM safety net for embedding spikes"
          grep -q "^$_swapfile " /etc/fstab 2>/dev/null \
            || echo "$_swapfile none swap sw 0 0" >> /etc/fstab 2>/dev/null \
            && ok "swap persisted to /etc/fstab" \
            || warn "swap active but not persisted — add to /etc/fstab: $_swapfile none swap sw 0 0"
        else
          warn "could not create 4 GiB swap at $_swapfile — set up manually to avoid OOM under embedding load"
        fi
      else
        warn "only ${_disk_free_mb:-0} MiB free on / — skipping swap bump (need >=5 GiB). Free disk then add 4 GiB swap manually to avoid OOM."
      fi
    else
      ok "swap already >= 4 GiB (${_swap_mb} MiB)"
    fi
  else
    warn "not root — skipping swap check. On a <4 GiB-swap box, add 4 GiB swap to avoid OOM under embedding load."
  fi

  _swappy=$(sysctl -n vm.swappiness 2>/dev/null || echo 60)
  if [ "$_swappy" -gt 10 ]; then
    sysctl -w vm.swappiness=10 >/dev/null 2>&1 && ok "vm.swappiness=10 applied (was $_swappy)" || warn "sysctl vm.swappiness=10 failed — run: sysctl -w vm.swappiness=10"
  else
    ok "vm.swappiness already <= 10 ($_swappy)"
  fi
  if ! grep -q "vm.swappiness" /etc/sysctl.conf 2>/dev/null; then
    echo "vm.swappiness=10" >> /etc/sysctl.conf && ok "vm.swappiness=10 persisted to /etc/sysctl.conf" || warn "could not write /etc/sysctl.conf — add manually: echo 'vm.swappiness=10' >> /etc/sysctl.conf"
  fi
fi

# ── Gateway resilience: auto-restart openclaw on crash/OOM ───────────────────
if [ "$(uname)" = "Linux" ] && command -v systemctl >/dev/null 2>&1; then
  _patched=0
  while IFS= read -r _unit; do
    _frag=$(systemctl show "$_unit" -p FragmentPath 2>/dev/null | cut -d= -f2)
    [ -z "$_frag" ] || [ ! -f "$_frag" ] && continue
    if grep -q "^Restart=" "$_frag"; then
      sed -i 's/^Restart=.*/Restart=on-failure/' "$_frag" && _patched=$((_patched+1))
    else
      # inject after [Service] section header
      sed -i '/^\[Service\]/a Restart=on-failure' "$_frag" && _patched=$((_patched+1))
    fi
    # ensure RestartSec is set
    grep -q "^RestartSec=" "$_frag" || sed -i '/^Restart=on-failure/a RestartSec=5s' "$_frag"
  done < <(systemctl list-unit-files --type=service --plain --no-legend 2>/dev/null | grep "^openclaw" | grep -v "autosnapshot\|autocommit" | awk '{print $1}')
  if [ "$_patched" -gt 0 ]; then
    systemctl daemon-reload 2>/dev/null || true
    ok "Restart=on-failure applied to $_patched openclaw unit(s) — daemon-reload done"
  else
    ok "No openclaw units found to patch (may not be running as systemd service)"
  fi
fi

# ── Internal watchdog: auto-install ──────────────────────────────────────────
# Copies internal-watchdog.sh to scripts/ and wires a crontab entry.
# No external infra needed — works on any box. Idempotent.
hr "Internal watchdog"
_wdog_src="$SKILL_DIR/docs/watchdog/internal-watchdog.sh"
_wdog_dst="$WS/scripts/gateway-watchdog.sh"
if [ -f "$_wdog_src" ]; then
  cp "$_wdog_src" "$_wdog_dst" && chmod +x "$_wdog_dst" && ok "gateway-watchdog.sh installed to scripts/" || warn "failed to copy gateway-watchdog.sh"
  _cron_line="*/5 * * * * bash $_wdog_dst >> $WS/logs/gateway-watchdog.log 2>&1"
  if crontab -l 2>/dev/null | grep -qF "gateway-watchdog.sh"; then
    ok "gateway-watchdog cron already present"
  else
    ( crontab -l 2>/dev/null; echo "$_cron_line" ) | crontab - \
      && ok "gateway-watchdog cron added (every 5 min)" \
      || warn "crontab add failed — add manually: $_cron_line"
  fi
else
  warn "internal-watchdog.sh not found at $_wdog_src — skipping auto-install"
fi

# ── External watchdog reminder ────────────────────────────────────────────────
if [ -z "${DINOMEM_WATCHDOG_CONFIGURED:-}" ]; then
  warn "EXTERNAL WATCHDOG NOT CONFIGURED — gateway may stay frozen during severe swap thrash."
  warn "ACTION REQUIRED: deploy an external health monitor for full coverage."
  warn "Templates: $(realpath "$SKILL_DIR/docs/watchdog/" 2>/dev/null || echo '<dinomem-base-dir>/docs/watchdog/')/{cloudflare-worker-template.js,generic-cron-template.sh}"
  warn "After setup: add DINOMEM_WATCHDOG_CONFIGURED=1 to your gateway env to suppress this warning."
fi

hr "done"
# ── Tiered "what works now" verdict (noob-smooth) ────────────────────────────
# A first-time user who sees a wall of yellow warnings assumes it's broken. It
# isn't: core memory works off plain files + the retrieval tool, independent of
# the OPTIONAL layers (TEI/Docker for faster semantic recall, extra ingest tools).
# Lead with a plain-language status so they know they're good.
echo "  \033[1;32m✔ dinomem is installed and your agent's memory works now.\033[0m"
echo "    Core memory (auto-save + memory_search recall) is active — no further setup needed."
if command -v docker >/dev/null 2>&1 && tei_healthy 2>/dev/null; then
  echo "    \033[32m✔ Fast semantic recall (TEI) is running.\033[0m"
else
  echo "    \033[2m○ Optional: faster semantic recall (TEI/Docker) is off — memory still works without it.\033[0m"
fi
echo ""
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
