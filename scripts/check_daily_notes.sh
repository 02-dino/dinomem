#!/usr/bin/env bash
# check_daily_notes.sh — zero-LLM pre-check for the "Daily Note Review" cron.
# Exits 0 if there are memory notes worth reviewing (LLM should run).
# Exits 1 if nothing to review (skip LLM, zero cost).
#
# SHARED SUPERSET: identical file in dinomem (base) and dinomem-neuron.
#
# Daily Note Review is the general note janitor: it verifies done_when, retires
# resolved notes, and GC's stale/abandoned ones. Its work is driven by notes
# existing at all — a note's done_when can flip via external state WITHOUT the
# file changing, and stale_after GC is purely time-based.
#
# FIX (2026-07-24): the OLD gate fired on bare _note_ existence, EVERY tick of
# the parent Project Cron Gate (*/15). With one long-lived open note sitting in
# status:design, that was true on every tick forever -> the full LLM review ran
# every 15 min and reported the SAME note/verdict (confirmed: identical Telegram
# posts 8:15pm/8:30pm). "Daily" Note Review was firing 96x/day.
#
# NEW gate: only run when EITHER
#   (a) a _note_ file's mtime is newer than the last recorded review run, OR
#   (b) it has been >= 24h since the last recorded run (preserves the
#       time-based stale_after/done_when GC guarantee -- daily cadence is
#       sufficient for time-based expiry; nothing time-based needs 15-min
#       granularity).
# State is a single epoch-seconds stamp file; touched only when we return 0
# (i.e. only when the LLM review actually runs), so cadence is measured from
# actual runs, not gate-checks.
#
# Cost: pure filesystem scan + one stat, zero LLM, zero network.

set -uo pipefail

WS="${OPENCLAW_WORKSPACE:-$HOME/.openclaw/workspace}"
MEMORY_DIR="$WS/memory"
STATE_FILE="$WS/scripts/.check_daily_notes.last_run"
MIN_INTERVAL_SECS=$(( 24 * 3600 ))  # daily floor for the time-based GC guarantee

[ -d "$MEMORY_DIR" ] || exit 1

# No _note_ files at all → nothing for the janitor to do, regardless of state.
any_note=1
for f in "$MEMORY_DIR"/_note_*.md; do
  [ -f "$f" ] && any_note=0 && break
done
[ "$any_note" -eq 0 ] || exit 1

last_run=0
if [ -f "$STATE_FILE" ]; then
  last_run="$(cat "$STATE_FILE" 2>/dev/null || echo 0)"
  case "$last_run" in (''|*[!0-9]*) last_run=0 ;; esac
fi
now_epoch=$(date -u +%s)

# (a) any note changed since the last recorded run?
changed=1
if [ "$last_run" -gt 0 ]; then
  for f in "$MEMORY_DIR"/_note_*.md; do
    [ -f "$f" ] || continue
    mtime=$(stat -c %Y "$f" 2>/dev/null || stat -f %m "$f" 2>/dev/null || echo 0)
    if [ "$mtime" -gt "$last_run" ]; then
      changed=0
      break
    fi
  done
else
  changed=0  # never run before -> treat as changed (first run)
fi

# (b) daily floor elapsed since last run?
age=$(( now_epoch - last_run ))
due=1
[ "$age" -ge "$MIN_INTERVAL_SECS" ] && due=0

if [ "$changed" -eq 0 ] || [ "$due" -eq 0 ]; then
  # Record the run stamp NOW (gate fires -> caller will trigger the LLM job).
  printf '%s\n' "$now_epoch" > "$STATE_FILE" 2>/dev/null || true
  exit 0
fi

exit 1
