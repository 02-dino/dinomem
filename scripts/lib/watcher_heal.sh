#!/usr/bin/env bash
# watcher_heal.sh — opt-in self-heal layer for dinomem-health-watcher.sh.  [v1]
#
# SOURCEABLE, not executable. The watcher sources it after its checks ran, then
# calls heal_run. Needs from the watcher: AGENTS, AGENT_WS, OC, LOG_FILE, DRY_RUN,
# log(), check_log_job(), check_gateway(), and GW_DOWN[agent]=1 for gateways the
# check found offline.
#
# WHY a separate lib: the watcher must stay a pure reporter on boxes that never
# opt in (default OFF), and the heal surface is the risky half — keeping it in one
# file makes "what can this thing touch" auditable at a glance.
#
# WHAT IT MAY DO (and nothing else — never openclaw.json, never git, never auth):
#   1. TEI      : start/restart a TEI container that fails doctor.sh, then
#                 tei_reconcile auto (zero-swap converge; restarts only if swapped).
#   2. rollup   : retrieval_log --rollup when kb/retrieval_log/health.json is >36h old.
#   3. graph    : re-launch memory_graph (detached, same wrapper as its cron) when
#                 its log is stale/erroring — neuron installs only.
#   4. gateway  : restart an offline gateway. DEFAULT = approve-first, because on a
#                 user box a restart is a window of unanswered chats. Max one
#                 restart per unit per run, cooldown per agent.
# 1-3 are idempotent and run directly once auto-heal is on.
#
# Knobs: DINOMEM_HEAL_GATEWAY=approve|auto|off (default approve)
#        DINOMEM_HEAL_GATEWAY_CMD   custom restart cmd, "{agent}" is substituted
#        DINOMEM_HEAL_GATEWAY_COOLDOWN_SEC (default 3600)
#        DINOMEM_HEAL_APPROVAL_TTL_SEC     (default 172800 = 48h)

HEAL_LINES=()

_HEAL_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_HEAL_PENDING="$OC/logs/dinomem-heal-pending"
_HEAL_APPROVED="$OC/logs/dinomem-heal-approved"
_HEAL_GW_STATE="$OC/logs/dinomem-heal-gateway.state"

_heal_note() {  # $1=scope $2=result text
  HEAL_LINES+=("  • $1: $2")
  log "HEAL $1: $2" >>"$LOG_FILE"
}
_heal_age_h() {  # hours since mtime, or empty if missing
  [ -e "$1" ] || return 0
  local now m
  now="$(date +%s)"
  m="$(stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null)" || return 0
  echo $(( (now - m) / 3600 ))
}

# ── 1. TEI ────────────────────────────────────────────────────────────────────
heal_tei() {
  command -v docker >/dev/null 2>&1 || return 0
  local ids   # by image, not name — same rule as tei_reconcile
  ids="$(docker ps -a --format '{{.ID}} {{.Image}}' 2>/dev/null | awk '$2 ~ /text-embeddings-inference/ {print $1}')"
  [ -z "$ids" ] && return 0   # no TEI on this box (remote embed / --no-docker) -> not ours
  if [ -f "$_HEAL_LIB_DIR/../doctor.sh" ] && ! bash "$_HEAL_LIB_DIR/../doctor.sh" --quiet >/dev/null 2>&1; then
    local c st
    for c in $ids; do
      st="$(docker inspect -f '{{.State.Status}}' "$c" 2>/dev/null)"
      if [ "$DRY_RUN" = true ]; then _heal_note TEI "down -> would docker $( [ "$st" = running ] && echo restart || echo start ) $c (dry-run)"; continue; fi
      if [ "$st" = running ]; then docker restart -t 20 "$c" >/dev/null 2>&1; else docker start "$c" >/dev/null 2>&1; fi
    done
    if [ "$DRY_RUN" != true ]; then
      sleep 15
      if bash "$_HEAL_LIB_DIR/../doctor.sh" --quiet >/dev/null 2>&1; then _heal_note TEI "was down -> restarted, /health OK ✅"
      else _heal_note TEI "was down -> restarted, still unhealthy ❌ (docker logs <container>)"; fi
    fi
  fi
  # Zero-swap converge every run: no-op (silent) when already clamped + swap-free.
  if [ -f "$_HEAL_LIB_DIR/tei_reconcile.sh" ]; then
    local out line dr=0
    [ "$DRY_RUN" = true ] && dr=1
    # shellcheck source=/dev/null
    out="$( . "$_HEAL_LIB_DIR/tei_reconcile.sh"; DRY_RUN=$dr tei_reconcile auto 2>&1 )"
    [ -n "$out" ] && printf '%s\n' "$out" >>"$LOG_FILE"
    # [skip] = already converged -> stay silent; anything else is worth a line.
    line="$(printf '%s\n' "$out" | grep -E '\[(ok|warn|plan)\]' | tail -1 | sed 's/^ *\[[a-z]*\] //')"
    [ -n "$line" ] && { _heal_note TEI "zero-swap reconcile: $line"; }
  fi
  return 0
}

# ── 2. retrieval rollup ──────────────────────────────────────────────────────
heal_rollup() {
  local agent="$1" ws="${AGENT_WS[$1]}" age
  [ -f "$ws/procedures/_retrieval_log.py" ] && [ -d "$ws/kb/retrieval_log" ] || return 0
  age="$(_heal_age_h "$ws/kb/retrieval_log/health.json")"
  [ -n "$age" ] && [ "$age" -le 36 ] && return 0
  if [ "$DRY_RUN" = true ]; then _heal_note "$agent" "retrieval health $([ -n "$age" ] && echo "${age}h old" || echo missing) -> would run --rollup (dry-run)"; return 0; fi
  if (cd "$ws" && timeout 600 python3 procedures/_retrieval_log.py --workspace "$ws" --rollup) >>"$LOG_FILE" 2>&1; then
    _heal_note "$agent" "retrieval health $([ -n "$age" ] && echo "${age}h old" || echo missing) -> rollup OK ✅"
  else
    _heal_note "$agent" "retrieval rollup failed ❌ (see $LOG_FILE)"
  fi
}

# ── 3. memory graph (neuron) ─────────────────────────────────────────────────
heal_graph() {
  local agent="$1" ws="${AGENT_WS[$1]}" sev cls
  [ -f "$ws/procedures/memory_graph.py" ] && [ -f "$ws/scripts/dinomem_run.sh" ] || return 0
  IFS='|' read -r sev _ cls <<< "$(check_log_job "$ws/logs/memory_graph.log" 30 memory_graph)"
  [ "$sev" = OK ] && return 0
  pgrep -f "$ws.*memory_graph.py" >/dev/null 2>&1 && return 0   # already running
  if [ "$DRY_RUN" = true ]; then _heal_note "$agent" "memory graph ${cls} -> would relaunch (dry-run)"; return 0; fi
  # Detached: a graph build can take an hour; the watcher must not block on it.
  ( cd "$ws" && DINOMEM_AGENT_ID="$agent" nohup timeout 7200 bash "$ws/scripts/dinomem_run.sh" heavy-embed "$ws" \
      python3 procedures/memory_graph.py >>"$ws/logs/memory_graph.log" 2>&1 & )
  _heal_note "$agent" "memory graph ${cls} -> relaunched in background ⏳"
}

# ── 4. gateway (approve-first) ───────────────────────────────────────────────
_heal_gw_cmd() {
  local aid="$1"
  if [ -n "${DINOMEM_HEAL_GATEWAY_CMD:-}" ]; then echo "${DINOMEM_HEAL_GATEWAY_CMD//\{agent\}/$aid}"; return; fi
  if command -v systemctl >/dev/null 2>&1; then
    local u
    for u in "openclaw-gateway-$aid.service" "openclaw-gateway.service"; do
      systemctl --user cat "$u" >/dev/null 2>&1 && { echo "systemctl --user restart $u"; return; }
      systemctl cat "$u" >/dev/null 2>&1 && { echo "systemctl restart $u"; return; }
    done
  fi
  command -v openclaw >/dev/null 2>&1 && echo "openclaw gateway restart"
}
_heal_gw_cooldown_ok() {
  local last
  last="$(awk -v a="$1" '$1==a {print $2}' "$_HEAL_GW_STATE" 2>/dev/null | tail -1)"
  [ -z "$last" ] || [ $(( $(date +%s) - last )) -ge "${DINOMEM_HEAL_GATEWAY_COOLDOWN_SEC:-3600}" ]
}
heal_gateways() {
  local mode="${DINOMEM_HEAL_GATEWAY:-approve}" down=() a
  [ "$mode" = off ] && return 0
  for a in "${AGENTS[@]}"; do [ -n "${GW_DOWN[$a]:-}" ] && down+=("$a"); done
  [ ${#down[@]} -eq 0 ] && { rm -f "$_HEAL_PENDING"; return 0; }
  local fp; fp="gateway:$(IFS=,; echo "${down[*]}")"
  if [ "$mode" = approve ]; then
    local ok=false ap_fp ap_ts
    if [ -f "$_HEAL_APPROVED" ] && read -r ap_fp ap_ts < "$_HEAL_APPROVED" && [ "$ap_fp" = "$fp" ] \
       && [ $(( $(date +%s) - ap_ts )) -le "${DINOMEM_HEAL_APPROVAL_TTL_SEC:-172800}" ]; then ok=true; fi
    if [ "$ok" != true ]; then
      [ "$DRY_RUN" = true ] || echo "$fp $(date +%s)" > "$_HEAL_PENDING"
      _heal_note gateway "offline: ${down[*]} -> needs approval. Run: bash $0 --approve-heal"
      return 0
    fi
  fi
  local done_cmds=" " cmd
  for a in "${down[@]}"; do
    cmd="$(_heal_gw_cmd "$a")"
    [ -z "$cmd" ] && { _heal_note "$a" "gateway offline, no known restart command (set DINOMEM_HEAL_GATEWAY_CMD)"; continue; }
    case "$done_cmds" in *" $cmd "*) continue ;; esac   # shared unit: once per run
    _heal_gw_cooldown_ok "$a" || { _heal_note "$a" "gateway offline, restart skipped (cooldown)"; continue; }
    if [ "$DRY_RUN" = true ]; then _heal_note "$a" "gateway offline -> would run: $cmd (dry-run)"; continue; fi
    done_cmds+="$cmd "
    echo "$a $(date +%s)" >> "$_HEAL_GW_STATE"
    eval "$cmd" >>"$LOG_FILE" 2>&1
    sleep 10
    case "$(check_gateway "$a")" in
      OK*) _heal_note "$a" "gateway restarted (${cmd}) -> online ✅" ;;
      *)   _heal_note "$a" "gateway restarted (${cmd}) -> still offline ❌" ;;
    esac
  done
  [ "$DRY_RUN" = true ] || rm -f "$_HEAL_PENDING" "$_HEAL_APPROVED"
}

# --approve-heal: approve exactly what is pending (fingerprint match), then the
# same watcher run applies it immediately — no waiting for the next cron.
heal_approve_pending() {
  local fp
  { [ -f "$_HEAL_PENDING" ] && read -r fp _ < "$_HEAL_PENDING"; } || { echo "No pending heal to approve ($_HEAL_PENDING)."; return 1; }
  echo "$fp $(date +%s)" > "$_HEAL_APPROVED"
  echo "Approved: $fp — applying now."
}

heal_run() {
  local a
  heal_tei
  for a in "${AGENTS[@]}"; do heal_rollup "$a"; heal_graph "$a"; done
  heal_gateways
  return 0
}

build_heal_block() {
  [ ${#HEAL_LINES[@]} -eq 0 ] && return 0
  printf '🩹 Auto-heal\n'; printf '%s\n' "${HEAL_LINES[@]}"
}
