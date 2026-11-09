#!/usr/bin/env bash
# tei_reconcile.sh — bring EXISTING TEI containers to the zero-swap contract.  [v1]
#
# SOURCEABLE, not executable:  . "$SKILL_DIR/scripts/lib/tei_reconcile.sh"; tei_reconcile [auto|force|never]
#
# WHY: compose memswap_limit / `docker run --memory-swap` only shape a container
# at CREATE time, and the installer reuses a healthy TEI instead of recreating
# it, so a box installed before zero-swap shipped keeps Docker's default
# (swap = 2x memory) forever. Measured 2026-11-09: e5 held 1.0GiB in swap and
# memory_search timed out at 15s. One function, called from install, --repair-cron
# (base + neuron) and the health watcher, so old boxes converge without a reinstall.
#
# CONTRACT, per running container whose image repo is text-embeddings-inference
# (by image, not name -> hand-made tei-rerank and legacy cpu-1.6 are covered):
#   1. clamp:   memswap := memory via `docker update` (live, no restart). Memory=0
#               (unbounded) is skipped — there is no limit to pin swap to.
#   2. flush:   `docker update` stops NEW swap but never pulls already-swapped pages
#               back. Restart ONLY when the container actually holds swap (VmSwap>0),
#               then wait for healthy. mode: auto (default) | force | never.
#               DINOMEM_TEI_NO_RESTART=1 forces never.
# Why conditional, not "restart every install": a 13-agent box runs the base
# installer 13x per rollout; unconditional = 13 recall outages. Conditional =
# at most one, because every later run sees swap already 0.
# A flock serializes concurrent callers so two agents never restart it at once.
# Always returns 0: TEI is optional infra, never fail an install over it.

_tr_log() { local lvl="$1"; shift
  if declare -F "$lvl" >/dev/null; then "$lvl" "$*"; else printf '  [%s] %s\n' "$lvl" "$*"; fi; }

_tr_wait_healthy() {  # $1=container, $2=timeout s. healthcheck if present, else running.
  local c="$1" t="${2:-90}" i s
  for ((i=0; i<t; i++)); do
    s="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$c" 2>/dev/null)"
    case "$s" in healthy|running) echo "$i"; return 0 ;; esac
    sleep 1
  done
  return 1
}

tei_reconcile() {
  local mode="${1:-${TEI_RESTART:-auto}}"
  [ "${DINOMEM_TEI_NO_RESTART:-0}" = 1 ] && mode=never
  command -v docker >/dev/null 2>&1 || return 0
  local ids; ids="$(docker ps --format '{{.ID}} {{.Image}}' 2>/dev/null | awk '$2 ~ /text-embeddings-inference/ {print $1}')"
  [ -n "$ids" ] || return 0

  local lockf="${TMPDIR:-/tmp}/dinomem-tei-reconcile.lock" fd
  if command -v flock >/dev/null 2>&1 && exec {fd}>"$lockf" 2>/dev/null; then
    flock -w 180 "$fd" || { _tr_log warn "TEI reconcile: another run holds the lock >180s — skipping"; exec {fd}>&-; return 0; }
  fi

  local c n m s pid sw secs
  for c in $ids; do
    read -r n m s pid <<<"$(docker inspect -f '{{.Name}} {{.HostConfig.Memory}} {{.HostConfig.MemorySwap}} {{.State.Pid}}' "$c" 2>/dev/null)"
    n="${n#/}"
    [ "${m:-0}" -gt 0 ] 2>/dev/null || { _tr_log skip "TEI $n: no memory limit — swap clamp N/A"; continue; }

    if [ "$s" != "$m" ]; then
      if [ "${DRY_RUN:-0}" = 1 ]; then _tr_log plan "docker update --memory-swap $m $n (disable swap; now $s)"
      elif docker update --memory-swap "$m" "$c" >/dev/null 2>&1; then _tr_log ok "TEI $n: swap disabled (memswap $s -> $m)"
      else _tr_log warn "TEI $n: docker update --memory-swap failed (non-fatal)"; fi
    fi

    sw="$(awk '/^VmSwap:/ {print $2}' "/proc/${pid:-0}/status" 2>/dev/null)"; sw="${sw:-0}"
    case "$mode" in
      never) [ "$sw" -gt 0 ] && _tr_log warn "TEI $n: ${sw} kB still swapped; restart disabled — flush with: docker restart $n"; continue ;;
      force) : ;;
      *)     [ "$sw" -gt 0 ] || { [ "$s" = "$m" ] && _tr_log skip "TEI $n: swap-free"; continue; } ;;
    esac

    if [ "${DRY_RUN:-0}" = 1 ]; then _tr_log plan "docker restart $n (mode=$mode, swapped ${sw} kB)"; continue; fi
    _tr_log warn "TEI $n: restarting to flush ${sw} kB swap (mode=$mode) — recall pauses ~25s"
    if docker restart -t 20 "$c" >/dev/null 2>&1 && secs="$(_tr_wait_healthy "$c" 90)"; then
      _tr_log ok "TEI $n: healthy again after ${secs}s"
    else
      _tr_log warn "TEI $n: not healthy within 90s after restart — check: docker logs $n"
    fi
  done
  [ -n "${fd:-}" ] && exec {fd}>&-
  return 0
}
