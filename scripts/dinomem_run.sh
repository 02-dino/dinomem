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

if _load_defer; then
  _log "DEFER: load too high for $CLASS (agent=$AGENT_ID, workspace=$WORKSPACE) — will retry next tick"
  exit 0
fi

# ── flock availability check ────────────────────────────────────────────────
if ! command -v flock >/dev/null 2>&1; then
  _log "WARN: flock not found — running $CLASS job unlocked (install util-linux to enable cross-agent serialization)"
  (cd "$WORKSPACE" && exec "$@")
  exit $?
fi

# ── acquire lock + run ───────────────────────────────────────────────────────
_log "acquiring $CLASS lock (timeout ${TIMEOUT}s, agent=$AGENT_ID): $*"
START=$(date -u +%s 2>/dev/null || echo 0)

RC=0
# flock --timeout: block up to $TIMEOUT seconds, then exit status 1.
# Subshell: fd is released the instant the command exits (even on SIGKILL).
(
  flock --timeout "$TIMEOUT" "$LOCK_FILE" \
    bash -c 'cd "$1"; shift; exec "$@"' -- "$WORKSPACE" "$@"
) || RC=$?

END=$(date -u +%s 2>/dev/null || echo 0)
ELAPSED=$(( END - START ))

if [ "$RC" -eq 0 ]; then
  _log "done: $CLASS lock released after ${ELAPSED}s (agent=$AGENT_ID)"
elif [ "$RC" -eq 1 ] && [ "$ELAPSED" -ge "$TIMEOUT" ]; then
  _log "WARN: $CLASS lock timed out after ${ELAPSED}s (another agent may be stuck) — running unlocked (fail-open)"
  (cd "$WORKSPACE" && exec "$@") || RC=$?
else
  _log "ERROR: $CLASS job exited $RC after ${ELAPSED}s (agent=$AGENT_ID)"
fi

exit $RC
