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
# file changing, and stale_after GC is purely time-based. So the ONLY safe
# "nothing to do" condition is: there are literally no _note_ files to janitor.
#
# We therefore gate on _note_ existence (broad, never misses time-based GC). The
# extra checks below are kept only as fast-path early exits for the common cases;
# the final catch-all is the authoritative gate.
#
# Cost: pure filesystem scan, zero LLM, zero network.

set -uo pipefail

WS="${OPENCLAW_WORKSPACE:-$HOME/.openclaw/workspace}"
MEMORY_DIR="$WS/memory"

[ -d "$MEMORY_DIR" ] || exit 1

# AUTHORITATIVE GATE: any _note_ file exists → there is something to janitor
# (verify done_when / stale_after GC / retire). This alone is sufficient and
# never misses a time-based retirement. Fast path: exit as soon as one is found.
for f in "$MEMORY_DIR"/_note_*.md; do
  [ -f "$f" ] && exit 0
done

# No _note_ files at all → nothing for the janitor to do.
exit 1
