#!/usr/bin/env bash
# check_improvement.sh — zero-LLM lane check for the auto self-improvement worker.
#
# CONTRACT (same as every other check_*.sh in this dir): exit 0 = there is work,
# exit 1 = nothing to do. cron_gate.sh only dispatches the LLM worker on exit 0,
# so an idle day costs nothing.
#
# WHY A CHECK SCRIPT AND NOT A gate/ TEMPLATE: the gate/*.sh templates feed
# cron_tool.py --gate, whose stdout is ANNOUNCED TO THE OWNER as a chat message.
# That is right for a digest the owner reads, and wrong here: this lane's payload
# is an instruction for the model, and the work needs a real agentTurn with tools
# (it reads skills, runs route.py, writes a proposal). So this follows the
# dispatcher pattern instead — check here, work in a disabled worker job.
#
# Two cheap steps, in this order:
#   1. scan  — detect candidates from sessions archived since the watermark.
#              First ever run only SEEDS the watermark and emits nothing, so a
#              fresh install never floods the queue with old history.
#   2. due   — is anything claimable? Deliberately does NOT claim: if the gate
#              claimed and the dispatch then failed, the candidate would sit
#              stranded until its TTL expired.
#
# ONCE PER DAY. cron_gate.sh ticks every 15 minutes, so without this stamp the
# worker would be dispatched up to 96x a day and the owner would be handed a
# proposal every quarter hour. The owner's cadence ruling is DAILY; this is
# where that ruling is actually enforced, not in the gate's schedule.
set -uo pipefail

WS="${OPENCLAW_WORKSPACE:-${DINOMEM_WORKSPACE:-$HOME/.openclaw/workspace}}"
PY="${DINOMEM_PY:-python3}"
export DINOMEM_WORKSPACE="$WS"

[ -d "$WS" ] || exit 1

STAMP="$WS/kb/improvement_queue/.last_dispatch"
mkdir -p "$(dirname "$STAMP")" 2>/dev/null || true
if [ -f "$STAMP" ] && [ "$(cat "$STAMP" 2>/dev/null)" = "$(date -u +%Y-%m-%d)" ]; then
  exit 1   # already dispatched today
fi

# Detect. Never fatal: a scan failure must not wedge the lane, and `due` below
# is the real decision point anyway.
"$PY" "$WS/procedures/improvement_scan.py" >>"$WS/logs/improvement_scan.log" 2>&1 || true

if "$PY" "$WS/procedures/improvement_propose.py" due >/dev/null 2>&1; then
  # Stamp only on a real dispatch, so a day with no candidates does not consume
  # the day's single slot.
  date -u +%Y-%m-%d >"$STAMP" 2>/dev/null || true
  exit 0
fi
exit 1
