#!/usr/bin/env bash
# cron_gate.sh — zero-LLM dispatcher (extensible gate pattern).
#
# PATTERN: Gate layer (this script) + Worker layer (disabled agentTurn crons).
#
# WHY: An agentTurn cron spins up a full LLM session BEFORE any internal check
# can run — so a naive design pays for a model call every tick just to decide
# "nothing to do." This script runs as a COMMAND cron (payload.kind = command)
# inside the Gateway process: pure bash, zero LLM cost. It runs cheap check
# scripts (file scans, exit 0/1). Only when a check reports work does it call
# `openclaw cron run <jobid>` to dispatch the real LLM worker.
#
# Worker crons have their own schedules DISABLED — they only ever fire when
# triggered here. `openclaw cron run` executes disabled jobs.
#
# Net effect: idle ticks cost $0. LLM wakes only when there is real work.
#
# EXTENDING: Add a new lane by:
#   1. Write a check script (exits 0 = work, 1 = nothing)
#   2. Add a new env var for the worker job ID (install.sh fills it)
#   3. Add a Lane block below (3 lines: if/check/trigger)
#   4. Disable the worker cron's own schedule
#
# SINGLE OWNER (2026-11-16): this file ships ONLY in dinomem (base). Neuron used
# to ship its own copy and overwrite this one; the two drifted (neuron's lacked
# the Pending Reminder + Auto Improve lanes, so on neuron boxes those env keys
# were dead), so neuron no longer ships cron_gate.sh at all and its test suite
# fails if the file reappears there. Neuron-only lanes live HERE, env-guarded and
# file-guarded: on a base-only box their GATE_*_ID is empty and their check script
# is absent, so they are skipped silently. One gate cron, one file, no duplicates.
#
# Lanes (all optional, enabled by their env var):
#   GATE_ADVANCER_ID           -> Project Advancer            [neuron]
#   GATE_IMPROVER_ID           -> Project Improver            [neuron]
#   GATE_DELETER_ID            -> Verified Note Deleter        [neuron]
#   GATE_IDEATOR_ID            -> Brainstorm Ideator           [neuron]
#   GATE_DAILY_NOTE_REVIEW_ID  -> Daily Note Review            [base]
#   GATE_PENDING_REMINDER_ID   -> Pending Note Reminder        [base]
#   GATE_AUTO_IMPROVE_ID       -> Auto Improve                 [base]
#   GATE_STALL_RESCUER_ID      -> Stall Rescuer                [base]
#
# Portable: workspace from $OPENCLAW_WORKSPACE; job IDs from env (install.sh fills
# them). If a job-id env var is empty, that lane is skipped silently.
#
# Output contract: stdout is ALWAYS exactly NO_REPLY (trigger info goes to
# logs/cron_gate.log, see the end of this file). Always exits 0 (a gate that itself errors must not spam failure
# alerts; per-lane failures are logged, not fatal).
set -uo pipefail

WS="${OPENCLAW_WORKSPACE:-$HOME/.openclaw/workspace}"
SCRIPTS="$WS/scripts"

# Optionally source the efficiency+safety primitive lib (gate_lib.sh). It is
# harness sugar: sourcing it exposes sensor_* / trigger_p / pick_batch /
# defer_if_busy / safe_config_write to any lane that wants adaptive or
# resource-aware behavior. FALLBACK-SAFE by design: the router keeps its OWN
# inline trigger() below and every base lane works WITHOUT the lib, so a
# missing/older lib never breaks the router (base installs that predate the lib
# still run). Failure to source is swallowed.
if [ -r "$SCRIPTS/lib/gate_lib.sh" ]; then
  # shellcheck source=lib/gate_lib.sh
  source "$SCRIPTS/lib/gate_lib.sh" 2>/dev/null || true
fi

# git drift-context helper (neuron ships _gate_git_ctx.sh). Best-effort and
# fire-path only; a missing/broken helper never breaks the $0 dispatcher.
GIT_CTX_ENABLED=0
if [ -f "$SCRIPTS/_gate_git_ctx.sh" ]; then
  # shellcheck source=/dev/null
  if source "$SCRIPTS/_gate_git_ctx.sh" 2>/dev/null && declare -F write_git_ctx >/dev/null 2>&1; then
    GIT_CTX_ENABLED=1
  fi
fi
GIT_CTX_REPO="${GIT_CTX_REPO:-/root/.openclaw}"

# Job IDs (env-injected; install.sh substitutes real values). Empty => skip lane.
ADVANCER_ID="${GATE_ADVANCER_ID:-}"
IMPROVER_ID="${GATE_IMPROVER_ID:-}"
DELETER_ID="${GATE_DELETER_ID:-}"
IDEATOR_ID="${GATE_IDEATOR_ID:-}"
STALL_RESCUER_ID="${GATE_STALL_RESCUER_ID:-}"
# Optional lanes — set these env vars to enable:
DAILY_NOTE_REVIEW_ID="${GATE_DAILY_NOTE_REVIEW_ID:-}"
PENDING_REMINDER_ID="${GATE_PENDING_REMINDER_ID:-}"
AUTO_IMPROVE_ID="${GATE_AUTO_IMPROVE_ID:-}"

# Locate the openclaw CLI (cron command jobs run with the Gateway's PATH, but be safe).
OPENCLAW_BIN="${OPENCLAW_BIN:-$(command -v openclaw || true)}"

triggered=()

trigger() {
  # $1 = human lane name, $2 = job id
  local name="$1" jobid="$2"
  if [ -z "$jobid" ] || [ -z "$OPENCLAW_BIN" ]; then
    return 0
  fi
  # Fire path only: git drift context for this lane (zero LLM, fail-open).
  if [ "$GIT_CTX_ENABLED" -eq 1 ]; then
    write_git_ctx "$name" "$GIT_CTX_REPO" || true
  fi
  if "$OPENCLAW_BIN" cron run "$jobid" >/dev/null 2>&1; then
    triggered+=("$name")
  else
    # non-fatal: log to stderr (captured in cron history) but keep going
    echo "cron_gate: failed to trigger $name ($jobid)" >&2
  fi
}

# Lane 1 — Project Advancer: run when an in_progress project note exists.
# (checks invoked via `bash <script>`, so file-exists -f is enough; no +x needed)
if [ -n "$ADVANCER_ID" ] && [ -f "$SCRIPTS/check_projects.sh" ]; then
  if bash "$SCRIPTS/check_projects.sh"; then
    trigger "advancer" "$ADVANCER_ID"
  fi
fi

# Lane 2 — Project Improver: run when a done-but-unverified project note exists.
if [ -n "$IMPROVER_ID" ] && [ -f "$SCRIPTS/check_improvable.sh" ]; then
  if bash "$SCRIPTS/check_improvable.sh"; then
    trigger "improver" "$IMPROVER_ID"
  fi
fi

# Lane 3 — Verified Note Deleter: run when a verified:true note exists.
if [ -n "$DELETER_ID" ] && [ -f "$SCRIPTS/check_deletable.sh" ]; then
  if bash "$SCRIPTS/check_deletable.sh"; then
    trigger "deleter" "$DELETER_ID"
  fi
fi

# Lane 3b — Brainstorm Ideator [neuron]: run when a type:brainstorm/status:design
# note exists. Pressure-tests THINKING and appends a critique; never builds/reaps.
if [ -n "$IDEATOR_ID" ] && [ -f "$SCRIPTS/check_ideator.sh" ]; then
  if bash "$SCRIPTS/check_ideator.sh"; then
    trigger "ideator" "$IDEATOR_ID"
  fi
fi

# Lane 4 — Daily Note Review (optional): run when memory/ has notes to janitor.
# Enable by setting GATE_DAILY_NOTE_REVIEW_ID in the cron job's env.
if [ -n "$DAILY_NOTE_REVIEW_ID" ] && [ -f "$SCRIPTS/check_daily_notes.sh" ]; then
  if bash "$SCRIPTS/check_daily_notes.sh"; then
    trigger "daily-note-review" "$DAILY_NOTE_REVIEW_ID"
  fi
fi

# Lane 5 — Pending Note Reminder (optional): run when a task_bound note is past
# its remind date. check_pending_notes.py exits 0 (JSON on stdout) when there is
# work, 1 when nothing qualifies. Enable via GATE_PENDING_REMINDER_ID.
if [ -n "$PENDING_REMINDER_ID" ] && [ -f "$SCRIPTS/check_pending_notes.py" ]; then
  if python3 "$SCRIPTS/check_pending_notes.py" >/dev/null 2>&1; then
    trigger "pending-reminder" "$PENDING_REMINDER_ID"
  fi
fi

# Lane 6 — Auto-Improve (optional): run when a session was flagged as possibly
# containing a durable lesson. check_improvement.sh scans for candidates and
# exits 0 only when one is claimable; it deliberately does NOT claim, so a
# dispatch that never reaches the model leaves the candidate available instead
# of stranded. Enable via GATE_AUTO_IMPROVE_ID.
if [ -n "$AUTO_IMPROVE_ID" ] && [ -f "$SCRIPTS/check_improvement.sh" ]; then
  if bash "$SCRIPTS/check_improvement.sh"; then
    trigger "auto-improve" "$AUTO_IMPROVE_ID"
  fi
fi

# Lane 7 — Stall Rescuer: a spawned subagent DIED (failed/timeout/ghost) and
# nothing resolved it. SESSION-driven, unlike lane 1: a dead worker's frozen note
# looks exactly like an intentional pause to check_projects.sh, and some dead
# workers have no note at all. check_stalls.sh records each stall in its ledger
# (fires once); stdout is the JSON list, discarded here because this dispatcher
# must only ever print NO_REPLY — the rescuer re-reads it via `stall_watch.py pending`.
if [ -n "$STALL_RESCUER_ID" ] && [ -f "$SCRIPTS/check_stalls.sh" ]; then
  if bash "$SCRIPTS/check_stalls.sh" >/dev/null; then
    trigger "stall-rescuer" "$STALL_RESCUER_ID"
  fi
fi

# stdout is ALWAYS exactly NO_REPLY. WHY: a trigger tick used to print
# "cron_gate: triggered advancer" -- plain text that the delivery layer treats as a
# message. If the live gate's delivery ever drifts from none to announce (measured
# 2026-11-07: spammed a Telegram topic every 15min), delivery:none was the ONLY thing
# holding that text back. Trigger info goes to the log so safety no longer depends on it.
if [ "${#triggered[@]}" -gt 0 ]; then
  mkdir -p "$WS/logs" 2>/dev/null || true
  echo "$(date -u +%FT%TZ) cron_gate: triggered ${triggered[*]}" >> "$WS/logs/cron_gate.log" 2>/dev/null || true
fi
echo "NO_REPLY"
exit 0
