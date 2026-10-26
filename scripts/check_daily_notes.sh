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
# v3 (this): the work-once/hash/interval guard logic is FACTORED OUT into
#   gate_lib.sh (guard_composite). This script keeps ONLY the note-selection
#   filtering (what is reviewable); the fire/skip decision is one guard_composite
#   call. Behavior is UNCHANGED from v2 — same fire/skip decisions.
#
# Cost: pure filesystem scan + cheap hash, zero LLM, zero network.

set -uo pipefail

WS="${OPENCLAW_WORKSPACE:-$HOME/.openclaw/workspace}"
SCRIPTS_DIR="$WS/scripts"
MEMORY_DIR="$WS/memory"
STATE_FILE="$SCRIPTS_DIR/.check_daily_notes.last_run"
MIN_INTERVAL_SECS=$(( 24 * 3600 ))  # daily floor for the time-based GC guarantee

# The gate harness primitives (work-once guard + sensors + triggers). Factored out
# into the shared lib so EVERY lane gets the same fail-open, zero-LLM floor.
# shellcheck source=lib/gate_lib.sh
[ -f "$SCRIPTS_DIR/lib/gate_lib.sh" ] && source "$SCRIPTS_DIR/lib/gate_lib.sh"

[ -d "$MEMORY_DIR" ] || exit 1

# claim_freshness: delegated to gate_lib.sh (gate__is_fresh_claimed / gate__claim_window)
# so all three gates + claim_note.sh share ONE window. The local 120-min copy that
# used to live here drifted from claim_note.sh's 30 min and stalled the Advancer
# for 7 weeks -- see gate__claim_window's WHY block. Fail-open shim: an old
# gate_lib without the helper keeps this gate working (never fresh-claimed).
now_epoch=$(date -u +%s)

is_fresh_claimed() {
  declare -F gate__is_fresh_claimed >/dev/null 2>&1 || return 1
  gate__is_fresh_claimed "$1"
}

# Note: the claim-excluding content hash now lives in gate_lib.sh (gate__body_hash),
# used by guard_composite below. It is NOT duplicated here.

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

# SEPARATION OF CONCERNS: the Advancer/Improver/Deleter pipeline is the PROJECT
# state machine — it owns type: project notes ONLY (Improver/C2 hard-filters to
# `type: project`; a non-project done note is never verified/deleted by it). So:
#   - type: project (any status) -> pipeline's lane -> NOT the janitor's work.
#   - NON-project status: done    -> pipeline orphan -> IS the janitor's work
#       (daily_note_review Rule 2b retires it with a snapshot). So it MUST stay
#       reviewable here, otherwise nobody ever fires the turn that retires it.
# Only project notes are skipped; everything else (incl. non-project done) is
# reviewable so genuine janitor work still triggers the LLM turn.
is_pipeline_owned() {
  local f="$1" typ
  typ=$(grep -E '^type:' "$f" | head -n1 | sed -E 's/^type:[[:space:]]*//' | tr -d '\r' | xargs || true)
  [ "$typ" = "project" ] && return 0   # project = pipeline lane; all else is reviewable
  return 1
}

for f in "$MEMORY_DIR"/_note_*.md; do
  [ -f "$f" ] || continue
  is_fresh_claimed "$f" && continue   # actively held -> not the janitor's business now
  is_pipeline_owned "$f" && continue  # project OR done -> pipeline's lane, not the janitor's
  reviewable+=("$f")
done

# Nothing reviewable (no notes, or all are live-claimed) -> nothing to do.
[ "${#reviewable[@]}" -gt 0 ] || exit 1

# Fire decision — work-once guard + interval floor, one call, from gate_lib.sh.
# guard_composite <state_file> <min_secs> <input...>:
#   = (content changed since last run? hash excludes claim lines)
#     AND (interval floor elapsed? — daily here).
#   Stamps "<epoch> <hash>" on fire only, so cadence is measured from real runs.
#   Behavior is byte-for-byte what this script's inlined v2 logic did.
if guard_composite "$STATE_FILE" "$MIN_INTERVAL_SECS" "${reviewable[@]}"; then
  exit 0
fi

exit 1
