#!/usr/bin/env bash
# dinomem_run.sh — cross-agent serialization wrapper for heavy dinomem cron jobs.
#
# Usage (from crontab — installed automatically by install.sh):
#   bash scripts/dinomem_run.sh <class> <workspace> <script> [args...]
#
# Classes:
#   heavy-llm   : LLM-calling jobs (memory_cleanup, memory_review,
#                 memory_synthesis, contradiction_check, confidence_engine,
#                 memory_promote, generate_topic_index)
#   heavy-embed : embedding/CPU-heavy jobs without LLM (memory_graph,
#                 docs_ingest, session_ingest)
#   light       : fast/self-fenced jobs (auto_session_reset, cleanup_startup_daily,
#                 workspace_backup, code_graph, _retrieval_log, weekly_stats)
#                 — passed through unlocked (per-agent fcntl already guards these)
#
# WHY
#   On a multi-agent host every agent runs the same heavy cron classes.
#   Install.sh stagger offsets (10-15 min) prevent exact collisions at small N,
#   but LLM + embedding jobs routinely run longer than their stagger window, and
#   the window shrinks as agents are added. This wrapper acquires a HOST-WIDE
#   flock keyed by JOB CLASS before running, so at most one heavy-llm (or one
#   heavy-embed) runs at a time across ALL agents on the box — no matter how many
#   are installed. Agents queue (not skip) so no work is lost.
#
# LOCK DIR: /run/dinomem-locks/ (tmpfs, auto-cleaned on reboot).
#   Fallback: ~/.dinomem/locks/ (persistent, safe on non-tmpfs boxes).
#
# TIMEOUT: DINOMEM_LOCK_TIMEOUT_SECS (default 5400 = 90 min). After timeout the
#   job runs ANYWAY (fail-open) — a stuck peer must never permanently starve work.
#
# LOAD GUARD: if /proc/loadavg avg1 > DINOMEM_LOAD_CEIL cores (default 1.5x),
#   the job is deferred THIS tick (exits 0, logs reason). The starvation floor
#   (min-interval guard in cron_gate.sh) ensures it retries within the hour.
#   Only applies to heavy-* classes; light jobs are never load-deferred.
#
# GATE RETRY + STARVATION ESCAPE (2026-09-05): both resource gates (the load
#   guard above AND check_resources.sh) used to bare-`exit 0` on a busy box, so a
#   ONCE-DAILY heavy job (memory_review/memory_graph/docs_ingest) that lost the
#   coin-flip lost a WHOLE DAY — and on a box whose load floors above the ratio it
#   starved permanently (real incident: analyst memory_review 8 days dead). Fix is
#   two-layer and PORTABLE (all thresholds relative-to-cores or time, self-scaling
#   to any box — a 2-core Pi and a 32-core server behave the same):
#     1. RETRY: on a gate skip, recheck up to DINOMEM_GATE_RETRIES times with
#        DINOMEM_GATE_RETRY_WAIT_S between (default 3 x 120s = up to 6 min). Load is
#        spiky, so a short wait usually catches a dip — cheap, no work lost.
#     2. STARVATION ESCAPE: track last successful run per (agent,class,cmd) in a
#        stamp file; if the job has been starved longer than
#        DINOMEM_GATE_MAX_STARVE_H (default 20h, safely under a 24h daily cycle),
#        FORCE the run regardless of load. Guarantees a daily job never loses a
#        full day on ANY box, however loaded. Fail-open: no /proc, no stamp dir,
#        unreadable load -> proceed (never silently lose work).
#
# FAIL-OPEN CONTRACT: this wrapper must NEVER silently lose work.
#   - flock absent → log warning, run unlocked.
#   - lock timeout → log warning, run unlocked (fail-open).
#   - load-defer → log reason, exit 0 (cron does not retry on 0 exit, but
#     the next scheduled tick retries naturally).

set -uo pipefail

CLASS="${DINOMEM_LOCK_CLASS:-${1:-light}}"; shift || true
WORKSPACE="${1:-.}"; shift || true
# remaining positional args: the command to run

LOCK_DIR=""
if [ -d /run ] && [ -w /run ]; then
  LOCK_DIR="/run/dinomem-locks"
else
  LOCK_DIR="${HOME:-/root}/.dinomem/locks"
fi
mkdir -p "$LOCK_DIR" 2>/dev/null || true

TIMEOUT="${DINOMEM_LOCK_TIMEOUT_SECS:-5400}"
LOAD_CEIL="${DINOMEM_LOAD_CEIL:-1.5}"
LOCK_FILE="$LOCK_DIR/${CLASS}.lock"
AGENT_ID="${DINOMEM_AGENT_ID:-unknown}"

_log() { echo "[dinomem_run] $*" >&2; }

# ── light class: pass through without locking ────────────────────────────────
if [ "$CLASS" = "light" ]; then
  (cd "$WORKSPACE" && exec "$@")
  exit $?
fi

# ── load guard (heavy classes only) ─────────────────────────────────────────
# Skip this tick if box is saturated. Uses /proc/loadavg (Linux); degrades
# gracefully on macOS/BusyBox (no /proc → always proceed).
_load_defer() {
  [ -r /proc/loadavg ] || return 1   # no /proc → don't defer
  local cores load1
  cores=$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 1)
  load1=$(awk '{print $1}' /proc/loadavg 2>/dev/null | tr -d ' ')
  [ -z "$load1" ] && return 1   # unreadable → don't defer
  local ceiling
  ceiling=$(awk -v c="$cores" -v l="$LOAD_CEIL" 'BEGIN{printf "%.2f", c*l}')
  # return 0 (defer) if load1 > ceiling
  awk -v l="$load1" -v c="$ceiling" 'BEGIN{exit (l > c ? 0 : 1)}'
}

# ── Gate retry + starvation escape (PORTABLE; see header) ────────────────────
# One shared loop wraps BOTH gates (load guard + check_resources.sh) so neither
# does a bare exit-0 skip. All thresholds are relative-to-cores or wall-clock, so
# this behaves the same on a 2-core Pi and a 32-core server.
GATE_RETRIES="${DINOMEM_GATE_RETRIES:-3}"
GATE_RETRY_WAIT_S="${DINOMEM_GATE_RETRY_WAIT_S:-120}"
GATE_MAX_STARVE_H="${DINOMEM_GATE_MAX_STARVE_H:-20}"
# Per-(agent,class,command) last-success stamp, in LOCK_DIR (tmpfs w/ persistent
# fallback — no new dir contract). Keyed by a cksum of agent+class+cmd so an
# agent's three heavy jobs don't share one stamp.
_gate_stamp() {
  local key; key=$(printf '%s|%s|%s' "$AGENT_ID" "$CLASS" "$*" | cksum | tr -cd '0-9' | cut -c1-16)
  printf '%s/gate-stamp-%s' "$LOCK_DIR" "$key"
}
# _starved: return 0 (escape) if this job has NOT succeeded within
# GATE_MAX_STARVE_H. Fail-open: no stamp / unreadable last-success => escape.
_starved() {
  local stamp last now; stamp="$(_gate_stamp "$@")"
  [ -f "$stamp" ] || return 0                # never ran / stamp gone -> escape
  last=$(cat "$stamp" 2>/dev/null || echo 0)
  now=$(date -u +%s 2>/dev/null || echo 0)
  { [ "$last" -gt 0 ] 2>/dev/null; } || return 0   # bad stamp -> escape
  { [ "$now"  -gt 0 ] 2>/dev/null; } || return 1   # no clock -> don't force on load
  awk -v n="$now" -v l="$last" -v h="$GATE_MAX_STARVE_H" 'BEGIN{exit ((n-l) > h*3600 ? 0 : 1)}'
}
# _gates_pass: return 0 iff BOTH gates currently allow the run.
_gates_pass() {
  _load_defer && return 1
  if [ -f "$(dirname "$0")/check_resources.sh" ]; then
    bash "$(dirname "$0")/check_resources.sh" "${CLASS:-}" >/dev/null 2>&1 || return 1
  fi
  return 0
}
_gate_try=0
while ! _gates_pass; do
  if _starved "$@"; then
    _log "FORCE: $CLASS starved > ${GATE_MAX_STARVE_H}h (agent=$AGENT_ID) — running despite load to avoid losing a full cycle"
    break
  fi
  if [ "$_gate_try" -ge "$GATE_RETRIES" ]; then
    _log "DEFER: gates busy after $GATE_RETRIES retries for $CLASS (agent=$AGENT_ID, workspace=$WORKSPACE) — retry next tick"
    exit 0
  fi
  _gate_try=$((_gate_try+1))
  _log "WAIT: gate busy for $CLASS (agent=$AGENT_ID), retry $_gate_try/$GATE_RETRIES in ${GATE_RETRY_WAIT_S}s"
  sleep "$GATE_RETRY_WAIT_S"
done
# Gates cleared (passed or forced). Stamp success so the NEXT run can measure
# starvation from here.
date -u +%s > "$(_gate_stamp "$@")" 2>/dev/null || true

# ── flock availability check ────────────────────────────────────────────────
if ! command -v flock >/dev/null 2>&1; then
  _log "WARN: flock not found — running $CLASS job unlocked (install util-linux to enable cross-agent serialization)"
  _BEFORE_MB=$(awk '/^MemAvailable:/ {printf "%d", $2/1024}' /proc/meminfo 2>/dev/null || echo 0)
  (cd "$WORKSPACE" && exec "$@")
  _RC=$?
  _AFTER_MB=$(awk '/^MemAvailable:/ {printf "%d", $2/1024}' /proc/meminfo 2>/dev/null || echo 0)
  [ -f "$(dirname "$0")/check_resources.sh" ] && bash "$(dirname "$0")/check_resources.sh" --record-usage "${CLASS:-light}" "$_BEFORE_MB" "$_AFTER_MB" 2>/dev/null || true
  exit $_RC
fi

# ── acquire lock + run ───────────────────────────────────────────────────────
_log "acquiring $CLASS lock (timeout ${TIMEOUT}s, agent=$AGENT_ID): $*"
START=$(date -u +%s 2>/dev/null || echo 0)
_BEFORE_MB=$(awk '/^MemAvailable:/ {printf "%d", $2/1024}' /proc/meminfo 2>/dev/null || echo 0)

RC=0
# flock --timeout: block up to $TIMEOUT seconds, then exit status 1.
# Subshell: fd is released the instant the command exits (even on SIGKILL).
(
  flock --timeout "$TIMEOUT" "$LOCK_FILE" \
    bash -c 'cd "$1"; shift; exec "$@"' -- "$WORKSPACE" "$@"
) || RC=$?

END=$(date -u +%s 2>/dev/null || echo 0)
ELAPSED=$(( END - START ))
_AFTER_MB=$(awk '/^MemAvailable:/ {printf "%d", $2/1024}' /proc/meminfo 2>/dev/null || echo 0)
[ -f "$(dirname "$0")/check_resources.sh" ] && bash "$(dirname "$0")/check_resources.sh" --record-usage "${CLASS:-light}" "$_BEFORE_MB" "$_AFTER_MB" 2>/dev/null || true

if [ "$RC" -eq 0 ]; then
  _log "done: $CLASS lock released after ${ELAPSED}s (agent=$AGENT_ID)"
elif [ "$RC" -eq 1 ] && [ "$ELAPSED" -ge "$TIMEOUT" ]; then
  _log "WARN: $CLASS lock timed out after ${ELAPSED}s (another agent may be stuck) — running unlocked (fail-open)"
  (cd "$WORKSPACE" && exec "$@") || RC=$?
else
  _log "ERROR: $CLASS job exited $RC after ${ELAPSED}s (agent=$AGENT_ID)"
fi

exit $RC
