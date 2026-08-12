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
# ── BUG HISTORY (read before touching the gate logic) ────────────────────────
# v0 (bug): gate fired on bare _note_ existence -> exit 0 EVERY */15 tick. With
#   one long-lived open note it ran the full LLM review 96x/day, same verdict.
# v1 (bug): gated on note MTIME vs a last_run stamp. Looked right, but a
#   SELF-PERPETUATING LOOP defeated it: the dinomem-open-notes bootstrap hook
#   auto-claims in_progress notes (rewrites `claimed_at:`), and the review worker
#   itself claims/refreshes — BOTH bump mtime a few seconds AFTER the gate's
#   stamp. So every tick "note mtime > last_run" was true again -> fired forever.
#   A claim-timestamp refresh is NOT a content change, but mtime can't tell them
#   apart. (Confirmed 2026-07-24: stamp=22:15:00, note mtime=22:15:11.)
# v2 (this): TWO fixes that together kill the loop —
#   (A) SKIP live-session-claimed notes entirely. A note actively held by a live
#       session is being worked by a human/agent RIGHT NOW; the janitor has no
#       business reviewing it every 15 min. (Mirrors the claim-awareness that
#       check_ideator.sh / check_improvable.sh already have — this gate lacked it.)
#   (B) Gate on a CONTENT HASH that EXCLUDES the claim lines (claimed_by/
#       claimed_at) instead of mtime. A pure claim refresh leaves the hash
#       unchanged -> gate stays quiet; a real edit changes it -> gate fires.
#   Plus the (b) daily-floor: run at least once / 24h regardless, so time-based
#   stale_after/done_when GC is never missed (daily is enough; nothing time-based
#   needs 15-min granularity).
#
# State = a stamp file holding "<epoch> <hash>" of the last run. Written only when
# we return 0, so cadence is measured from actual runs, not gate-checks.
#
# Cost: pure filesystem scan + cheap hash, zero LLM, zero network.

set -uo pipefail

WS="${OPENCLAW_WORKSPACE:-$HOME/.openclaw/workspace}"
MEMORY_DIR="$WS/memory"
STATE_FILE="$WS/scripts/.check_daily_notes.last_run"
MIN_INTERVAL_SECS=$(( 24 * 3600 ))  # daily floor for the time-based GC guarantee

[ -d "$MEMORY_DIR" ] || exit 1

# claim_freshness windows (must match check_ideator.sh / check_improvable.sh):
#   live-session* -> 120 min (2h) lease; other claimants -> 30 min.
LIVE_WINDOW=7200
OTHER_WINDOW=1800
now_epoch=$(date -u +%s)

# is a note FRESHLY claimed by another worker/live session? (skip it if so)
is_fresh_claimed() {
  local f="$1" cb ca ce age window
  cb=$(grep -E '^claimed_by:'  "$f" | tail -n1 | sed -E 's/^claimed_by:[[:space:]]*//'  | tr -d '\r' | xargs || true)
  ca=$(grep -E '^claimed_at:'  "$f" | tail -n1 | sed -E 's/^claimed_at:[[:space:]]*//'  | tr -d '\r' | xargs || true)
  [ -n "$cb" ] && [ -n "$ca" ] || return 1          # no claim -> not fresh-claimed
  ce=$(date -u -d "$ca" +%s 2>/dev/null) || return 1 # unparsable -> treat as not claimed
  age=$(( now_epoch - ce ))
  case "$cb" in live-session*) window=$LIVE_WINDOW ;; *) window=$OTHER_WINDOW ;; esac
  [ "$age" -lt "$window" ]                            # 0 (true) = fresh claim -> skip
}

# content hash of a note EXCLUDING the volatile claim lines, so a claimed_at
# refresh does not look like a content change.
note_body_hash() {
  grep -vE '^claimed_(by|at):' "$1" 2>/dev/null | sha256sum 2>/dev/null | cut -d' ' -f1
}

# Build the set of REVIEWABLE notes (exist AND not freshly claimed by another).
reviewable=()
# is this an in_progress PROJECT note? if so it is the Advancer's lane, not the
# janitor's. Its body (current_step/resume_state) changes every build turn,
# which defeats the content-hash guard below and re-fires the review each tick
# with the same KEEP verdict (spam, confirmed 2026-08-13). The janitor only
# needs a project note once it is DONE (to verify done_when + retire) or stale
# (time-based GC) -- never while it is actively advancing.
is_active_project() {
  local f="$1" typ st
  typ=$(grep -E '^type:'   "$f" | head -n1 | sed -E 's/^type:[[:space:]]*//'   | tr -d '\r' | xargs || true)
  st=$(grep -E '^status:' "$f" | head -n1 | sed -E 's/^status:[[:space:]]*//' | tr -d '\r' | xargs || true)
  [ "$typ" = "project" ] && [ "$st" = "in_progress" ]
}

for f in "$MEMORY_DIR"/_note_*.md; do
  [ -f "$f" ] || continue
  is_fresh_claimed "$f" && continue   # actively held -> not the janitor's business now
  is_active_project "$f" && continue  # in_progress project -> Advancer's lane, skip (body churns, defeats hash)
  reviewable+=("$f")
done

# Nothing reviewable (no notes, or all are live-claimed) -> nothing to do.
[ "${#reviewable[@]}" -gt 0 ] || exit 1

# Aggregate content hash over all reviewable notes (order-stable via sort).
agg_hash=$(
  for f in "${reviewable[@]}"; do
    printf '%s %s\n' "$(basename "$f")" "$(note_body_hash "$f")"
  done | sort | sha256sum | cut -d' ' -f1
)

# Read prior state: "<epoch> <hash>".
last_run=0
last_hash=""
if [ -f "$STATE_FILE" ]; then
  read -r last_run last_hash < "$STATE_FILE" 2>/dev/null || true
  case "$last_run" in (''|*[!0-9]*) last_run=0 ;; esac
fi

# (A) content changed since last run? (claim refreshes excluded by the hash)
changed=1
if [ "$last_run" -gt 0 ] && [ -n "$last_hash" ]; then
  [ "$agg_hash" != "$last_hash" ] && changed=0
else
  changed=0   # never run before -> first run fires
fi

# (b) daily floor elapsed?
age=$(( now_epoch - last_run ))
due=1
[ "$age" -ge "$MIN_INTERVAL_SECS" ] && due=0

if [ "$changed" -eq 0 ] || [ "$due" -eq 0 ]; then
  printf '%s %s\n' "$now_epoch" "$agg_hash" > "$STATE_FILE" 2>/dev/null || true
  exit 0
fi

exit 1
