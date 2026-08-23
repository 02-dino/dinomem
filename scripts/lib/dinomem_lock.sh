#!/usr/bin/env bash
# dinomem_lock.sh — cross-agent serialization for heavy dinomem cron jobs.  [v1]
#
# SOURCEABLE bash library. NOT executable directly. Source it from any cron
# wrapper or check script:
#   source "$SCRIPTS/lib/dinomem_lock.sh"
#
# PURPOSE
#   On a multi-agent host (analyst + kttal + konsultan + ...) every agent runs
#   the same heavy cron classes (memory_cleanup, memory_review, memory_synthesis,
#   memory_graph, docs_ingest). The system crontab is per-USER, shared across all
#   agents. Install.sh stagger offsets (10–15 min between agents) prevent exact
#   collisions at small N, but:
#     - LLM + embedding jobs routinely take longer than their stagger window.
#     - Each agent added shrinks the stagger budget (N agents → 15/N min gap).
#     - At 4+ agents the stagger window is ≤ 3 min — guaranteed overlap.
#     - No existing mechanism serializes ACROSS agents; per-agent fcntl locks
#       (auto_session_reset.py) only prevent self-overlap.
#
# SOLUTION: kernel-backed flock on a SHARED, JOB-CLASS lock file.
#   - Lock dir: /run/dinomem-locks/ (tmpfs, auto-cleaned on reboot, no cleanup
#     needed). Fallback: ~/.dinomem/locks/ (persistent, safe on non-tmpfs boxes).
#   - Lock class = JOB TYPE (heavy-llm, heavy-embed, light), not per-agent and
#     not per-script. This serializes at the right granularity: two agents running
#     memory_synthesis and memory_review simultaneously are BOTH heavy-llm and
#     contend for the SAME lock, so only one runs at a time.
#   - Timeout: 90 min max wait (DINOMEM_LOCK_TIMEOUT_SECS, default 5400). A stuck
#     job releases on process exit (flock is tied to the fd). The timeout prevents
#     permanent starvation if the holding process hangs.
#   - Load guard: dinomem_run_locked() calls gate_lib's defer_if_busy if it is
#     available, adding a CPU backpressure layer on top of the serialization.
#
# CLASSES
#   heavy-llm   : jobs that call an LLM (memory_cleanup, memory_review,
#                 memory_synthesis, contradiction_check, confidence_engine,
#                 memory_promote, generate_topic_index)
#   heavy-embed : jobs that load/run the embedding model (docs_ingest,
#                 session_ingest, memory_graph — no LLM but CPU/RAM heavy)
#   light       : auto_session_reset, cleanup_startup_daily, workspace_backup,
#                 code_graph, _retrieval_log, weekly_stats (no lock needed;
#                 these are already per-agent fcntl-guarded or trivially fast)
#
# USAGE (from a cron line via scripts/dinomem_run.sh):
#   bash scripts/dinomem_run.sh heavy-llm <workspace> python3 procedures/memory_cleanup.py
#
# USAGE (sourceable):
#   source "$SCRIPTS/lib/dinomem_lock.sh"
#   dinomem_run_locked heavy-llm "$WS" python3 procedures/memory_review.py
#
# CROSS-CUTTING CONTRACTS (every function):
#   - fail-open: a lock-acquisition failure logs a warning and RUNS ANYWAY.
#     A broken lock must NEVER silently skip real work.
#   - zero-LLM in this file: locking decisions never call a model.
#   - set -uo pipefail friendly: functions RETURN, don't exit.
#   - POSIX flock: uses `flock` (util-linux). macOS ships it since 10.9.
#     BusyBox (Alpine) ships it. If absent, runs unlocked + warns once.

set -uo pipefail 2>/dev/null || true

# ── Lock directory resolution ──────────────────────────────────────────────
# Prefer /run (tmpfs, no cleanup needed, 0 disk writes on reboot) → ~/.dinomem/locks
_dinomem_lock_dir() {
  local d
  if [ -d /run ] && [ -w /run ]; then
    d="/run/dinomem-locks"
  else
    d="${HOME:-/root}/.dinomem/locks"
  fi
  mkdir -p "$d" 2>/dev/null || true
  echo "$d"
}

# ── dinomem_run_locked <class> <workspace> <cmd> [args...] ────────────────
# Acquire the class-level flock, then exec <cmd> [args...] under it.
# <workspace> is used only for log context (not a lock scope).
# Returns the exit code of <cmd>, or 0 if flock itself failed (fail-open).
#
# TIMEOUT env override: DINOMEM_LOCK_TIMEOUT_SECS (default 5400 = 90 min).
# CLASS override: DINOMEM_LOCK_CLASS overrides the positional <class> argument
# (allows the wrapping cron line to force a class without editing the script).
dinomem_run_locked() {
  local class="${DINOMEM_LOCK_CLASS:-$1}"; shift
  local workspace="$1"; shift
  # remaining args: the command to run

  local lock_dir; lock_dir="$(_dinomem_lock_dir)"
  local lock_file="$lock_dir/${class}.lock"
  local timeout_secs="${DINOMEM_LOCK_TIMEOUT_SECS:-5400}"

  # ── sanity: flock must be available ───────────────────────────────────────
  if ! command -v flock >/dev/null 2>&1; then
    echo "[dinomem_lock] WARN: flock not found — running $* WITHOUT cross-agent lock (install util-linux to fix)" >&2
    (cd "$workspace" && exec "$@")
    return $?
  fi

  # ── optional load-backpressure via gate_lib ────────────────────────────────
  # defer_if_busy is defined when gate_lib.sh is already sourced in the caller.
  # Only skip for light-class (never delay those); for heavy classes, if the box
  # is saturated, log and still proceed (fail-open; flock already serializes).
  if [ "$class" != "light" ] && declare -F defer_if_busy >/dev/null 2>&1; then
    if ! defer_if_busy "" "1.5"; then
      echo "[dinomem_lock] INFO: high load detected before acquiring $class lock — proceeding anyway (flock will serialize)" >&2
    fi
  fi

  # ── acquire the lock + run ────────────────────────────────────────────────
  echo "[dinomem_lock] acquiring $class lock (timeout ${timeout_secs}s): $*" >&2
  local start_ts; start_ts=$(date -u +%s 2>/dev/null || echo 0)

  # flock --timeout: wait up to $timeout_secs for the lock, then give up.
  # -w = --wait (POSIX flock). We wrap in a subshell so the flock fd is released
  # exactly when the command exits (even on SIGKILL via the OS fd cleanup).
  local rc=0
  (
    exec flock --timeout "$timeout_secs" "$lock_file" \
      bash -c 'cd "$1"; shift; exec "$@"' -- "$workspace" "$@"
  ) || rc=$?

  local end_ts; end_ts=$(date -u +%s 2>/dev/null || echo 0)
  local elapsed=$(( end_ts - start_ts ))

  if [ "$rc" -eq 0 ]; then
    echo "[dinomem_lock] $class lock released after ${elapsed}s: $* (exit 0)" >&2
  elif [ "$rc" -eq 1 ] && [ "$elapsed" -ge "$timeout_secs" ]; then
    echo "[dinomem_lock] WARN: $class lock timed out after ${elapsed}s — another agent may be stuck. Ran without lock (fail-open): $*" >&2
    # Fail-open: run anyway after timeout so work is not permanently lost.
    (cd "$workspace" && exec "$@") || rc=$?
  else
    echo "[dinomem_lock] $class lock: command exited $rc after ${elapsed}s: $*" >&2
  fi

  return $rc
}

# ── dinomem_lock_status ────────────────────────────────────────────────────
# Print which locks are currently held (advisory — uses lsof or /proc/locks).
# Informational only; never blocks or errors.
dinomem_lock_status() {
  local lock_dir; lock_dir="$(_dinomem_lock_dir)"
  echo "dinomem lock dir: $lock_dir"
  for f in "$lock_dir"/*.lock 2>/dev/null; do
    [ -f "$f" ] || continue
    local base; base="$(basename "$f")"
    if command -v flock >/dev/null 2>&1; then
      # try a non-blocking test acquire: rc 1 = locked, rc 0 = free
      if flock --nonblock "$f" true 2>/dev/null; then
        echo "  $base: FREE"
      else
        echo "  $base: HELD"
      fi
    else
      echo "  $base: (flock absent — cannot check)"
    fi
  done
}
