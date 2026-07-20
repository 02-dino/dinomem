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
# SUPERSET FILE: this same cron_gate.sh ships in BOTH dinomem (base) and
# dinomem-neuron. Every lane is env-guarded — if a lane's GATE_*_ID env var is
# empty, that lane is skipped silently. So a base-only install (which only sets
# the note_review + pending_reminder ids) runs 2 lanes; a neuron install (which
# also sets the project-trio ids) runs all 5 — from ONE gate cron, no duplicates.
#
# Lanes (all optional, enabled by their env var):
#   GATE_ADVANCER_ID           -> Project Advancer            [neuron]
#   GATE_IMPROVER_ID           -> Project Improver            [neuron]
#   GATE_DELETER_ID            -> Verified Note Deleter        [neuron]
#   GATE_DAILY_NOTE_REVIEW_ID  -> Daily Note Review            [base + neuron]
#   GATE_PENDING_REMINDER_ID   -> Pending Note Reminder        [base + neuron]
#
# Portable: workspace from $OPENCLAW_WORKSPACE; job IDs from env (install.sh fills
# them). If a job-id env var is empty, that lane is skipped silently.
#
# Output contract: prints a short line per lane it TRIGGERED. If it triggered
# nothing, prints exactly NO_REPLY so the command-cron delivery layer suppresses
# any announce. Always exits 0 (a gate that itself errors must not spam failure
# alerts; per-lane failures are logged, not fatal).
set -uo pipefail

WS="${OPENCLAW_WORKSPACE:-$HOME/.openclaw/workspace}"
SCRIPTS="$WS/scripts"

# Job IDs (env-injected; install.sh substitutes real values). Empty => skip lane.
ADVANCER_ID="${GATE_ADVANCER_ID:-}"
IMPROVER_ID="${GATE_IMPROVER_ID:-}"
DELETER_ID="${GATE_DELETER_ID:-}"
# Optional lanes — set these env vars to enable:
DAILY_NOTE_REVIEW_ID="${GATE_DAILY_NOTE_REVIEW_ID:-}"
PENDING_REMINDER_ID="${GATE_PENDING_REMINDER_ID:-}"

# Locate the openclaw CLI (cron command jobs run with the Gateway's PATH, but be safe).
OPENCLAW_BIN="${OPENCLAW_BIN:-$(command -v openclaw || true)}"

triggered=()

trigger() {
  # $1 = human lane name, $2 = job id
  local name="$1" jobid="$2"
  if [ -z "$jobid" ] || [ -z "$OPENCLAW_BIN" ]; then
    return 0
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

if [ "${#triggered[@]}" -eq 0 ]; then
  echo "NO_REPLY"
else
  echo "cron_gate: triggered ${triggered[*]}"
fi
exit 0
