#!/usr/bin/env bash
# check_resources.sh — dynamic resource gate for dinomem cron jobs.
# Returns 0 (ok to proceed) or 1 (skip this run, retry next cycle).
#
# Design: purely relative thresholds — no hardcoded MB values.
#   RAM check: available_pct = available/total. Threshold = max(MIN_FREE_PCT%, measured p90 of this job's consumption).
#   CPU check: load_avg_1m / nproc > MAX_LOAD_RATIO
#
# Per-job consumption tracking: each caller passes a JOB_ID. After a run,
# the caller optionally calls: check_resources.sh --record-usage <JOB_ID> <before_avail_mb> <after_avail_mb>
# History stored in /tmp/dinomem_resource_history/<JOB_ID>.log (10 entries rolling).
# p90 of recorded consumption used as the RAM threshold on subsequent runs.
#
# Env overrides (optional):
#   DINOMEM_MIN_FREE_PCT   — minimum free RAM % (default: 15)
#   DINOMEM_MAX_LOAD_RATIO — max load_avg_1m / nproc (default: 0.8)
#   DINOMEM_RESOURCE_DEBUG — set to 1 for verbose output
#
# Usage:
#   check_resources.sh [JOB_ID]           # check only
#   check_resources.sh --record-usage JOB_ID BEFORE_MB AFTER_MB  # record consumption
#   check_resources.sh --dry-run          # print thresholds and exit 0

set -euo pipefail

DEBUG="${DINOMEM_RESOURCE_DEBUG:-0}"
MIN_FREE_PCT="${DINOMEM_MIN_FREE_PCT:-15}"
MAX_LOAD_RATIO="${DINOMEM_MAX_LOAD_RATIO:-0.8}"
HISTORY_DIR="/tmp/dinomem_resource_history"

log() { [ "$DEBUG" = "1" ] && echo "[check_resources] $*" >&2 || true; }

# ── Record usage mode ─────────────────────────────────────────────────────────
if [ "${1:-}" = "--record-usage" ]; then
  JOB_ID="${2:?usage: --record-usage JOB_ID BEFORE_MB AFTER_MB}"
  BEFORE="${3:?}"
  AFTER="${4:?}"
  CONSUMED=$(( BEFORE - AFTER ))
  [ "$CONSUMED" -lt 0 ] && CONSUMED=0
  mkdir -p "$HISTORY_DIR"
  HIST_FILE="$HISTORY_DIR/${JOB_ID}.log"
  echo "$CONSUMED" >> "$HIST_FILE"
  # keep last 10 entries
  tail -10 "$HIST_FILE" > "${HIST_FILE}.tmp" && mv "${HIST_FILE}.tmp" "$HIST_FILE"
  log "recorded ${CONSUMED}MB for ${JOB_ID}"
  exit 0
fi

# ── Dry-run mode ──────────────────────────────────────────────────────────────
DRY_RUN=0
JOB_ID=""
if [ "${1:-}" = "--dry-run" ]; then
  DRY_RUN=1
elif [ -n "${1:-}" ]; then
  JOB_ID="$1"
fi

# ── Read system state ─────────────────────────────────────────────────────────
if [ "$(uname)" != "Linux" ]; then
  log "non-Linux: skipping resource check"
  exit 0
fi

TOTAL_MB=$(awk '/^MemTotal:/ {printf "%d", $2/1024}' /proc/meminfo)
AVAIL_MB=$(awk '/^MemAvailable:/ {printf "%d", $2/1024}' /proc/meminfo)
AVAIL_PCT=$(( AVAIL_MB * 100 / TOTAL_MB ))
NPROC=$(nproc 2>/dev/null || echo 1)
LOAD_1M=$(awk '{print $1}' /proc/loadavg)
# integer comparison: multiply by 100 to avoid bc dependency
LOAD_INT=$(echo "$LOAD_1M" | awk '{printf "%d", $1 * 100}')
LOAD_THRESHOLD_INT=$(echo "$MAX_LOAD_RATIO $NPROC" | awk '{printf "%d", $1 * $2 * 100}')

# ── Compute RAM threshold ─────────────────────────────────────────────────────
# Base threshold: MIN_FREE_PCT of total RAM
BASE_THRESHOLD_MB=$(( TOTAL_MB * MIN_FREE_PCT / 100 ))

# Per-job p90 threshold (if history exists)
P90_MB=0
if [ -n "$JOB_ID" ] && [ -f "$HISTORY_DIR/${JOB_ID}.log" ]; then
  # p90: sort numerically, take 90th percentile entry
  ENTRIES=$(wc -l < "$HISTORY_DIR/${JOB_ID}.log")
  if [ "$ENTRIES" -ge 3 ]; then
    P90_IDX=$(( ENTRIES * 90 / 100 + 1 ))
    P90_MB=$(sort -n "$HISTORY_DIR/${JOB_ID}.log" | sed -n "${P90_IDX}p")
    P90_MB="${P90_MB:-0}"
  fi
fi

# Threshold = max(base, p90) — but cap at 40% of total (sanity)
MAX_SANE=$(( TOTAL_MB * 40 / 100 ))
RAM_THRESHOLD_MB=$(( BASE_THRESHOLD_MB > P90_MB ? BASE_THRESHOLD_MB : P90_MB ))
[ "$RAM_THRESHOLD_MB" -gt "$MAX_SANE" ] && RAM_THRESHOLD_MB=$MAX_SANE

# ── Dry-run output ────────────────────────────────────────────────────────────
if [ "$DRY_RUN" = "1" ]; then
  echo "check_resources dry-run:"
  echo "  total_ram=${TOTAL_MB}MB  available=${AVAIL_MB}MB (${AVAIL_PCT}%)"
  echo "  ram_threshold=${RAM_THRESHOLD_MB}MB (base=${BASE_THRESHOLD_MB}MB p90=${P90_MB}MB)"
  echo "  load_1m=${LOAD_1M}  nproc=${NPROC}  load_threshold_ratio=${MAX_LOAD_RATIO}"
  echo "  job_id=${JOB_ID:-none}"
  exit 0
fi

# ── RAM gate ──────────────────────────────────────────────────────────────────
if [ "$AVAIL_MB" -lt "$RAM_THRESHOLD_MB" ]; then
  echo "[check_resources] SKIP: available RAM ${AVAIL_MB}MB < threshold ${RAM_THRESHOLD_MB}MB (${AVAIL_PCT}% free, total ${TOTAL_MB}MB). Retry next cycle." >&2
  exit 1
fi

# ── CPU gate ─────────────────────────────────────────────────────────────────
if [ "$LOAD_INT" -gt "$LOAD_THRESHOLD_INT" ]; then
  echo "[check_resources] SKIP: load ${LOAD_1M} on ${NPROC} cores > ratio ${MAX_LOAD_RATIO}. Retry next cycle." >&2
  exit 1
fi

log "OK: RAM ${AVAIL_MB}MB free (threshold ${RAM_THRESHOLD_MB}MB), load ${LOAD_1M}/${NPROC}"
exit 0
