#!/usr/bin/env bash
# dinomem worker-leak-guard — trim leaked OpenClaw inference workers.
#
# WHAT LEAKS AND WHY
#   dinomem calls the OpenClaw CLI with a timeout. Python's
#   subprocess.run(timeout=N) kills ONLY the direct child, but that child (the
#   `openclaw` CLI) spawns its own `openclaw-infer` worker holding 300-600MB.
#   When the timeout fires, the CLI dies and the worker survives, reparented to
#   init (ppid=1) with no caller left to read its output: pure wasted RAM.
#
#   Measured on an 11-agent box (2026-10-19): ~2.3 orphans/min, 22 alive at
#   once (~8GB), load average 81 on 8 cores, which in turn made a gateway miss
#   its shutdown deadline and get SIGKILLed mid-write.
#
#   dinomem >= this release fixes the SOURCE (process-group kill in
#   procedures/extract_memory.py). This guard is the SAFETY NET for the same
#   class of bug: any other path that leaks a worker, a version skew, or a
#   partially-updated install.
#
# WHY ORPHANS ARE SAFE TO KILL
#   ppid=1 means the spawning process is gone, so nothing is waiting on this
#   worker's output — it is already discarded. Killing it cannot truncate a
#   live reply. Workers with a LIVING parent are never touched, at any age or
#   size. This is the whole safety argument; keep it if you edit the matching.
#
#   The age floor exists only to avoid racing a worker whose parent died
#   milliseconds ago. Default 3m is derived from the upstream 120s CLI timeout
#   (an orphan from that path is >2m old by construction).
#
# Usage:
#   bash worker-leak-guard.sh [--dry-run] [--min-age-min N] [--patterns "a b"]
#
# Options:
#   --dry-run         report what would be killed, kill nothing
#   --min-age-min N   orphan age floor in minutes (default 3; env ORPHAN_MIN_MIN)
#   --patterns "..."  space-separated process-name patterns
#                     (default: "openclaw-infer openclaw-message")
#
# Exit status is 0 even when nothing is killed — this is a janitor, not a test.
set -uo pipefail

ORPHAN_MIN_MIN="${ORPHAN_MIN_MIN:-3}"
PATTERNS="${WORKER_LEAK_PATTERNS:-openclaw-infer openclaw-message}"
DRY_RUN=0

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run)       DRY_RUN=1; shift ;;
    --min-age-min)   ORPHAN_MIN_MIN="$2"; shift 2 ;;
    --min-age-min=*) ORPHAN_MIN_MIN="${1#*=}"; shift ;;
    --patterns)      PATTERNS="$2"; shift 2 ;;
    --patterns=*)    PATTERNS="${1#*=}"; shift ;;
    -h|--help)       grep -E '^#( |$)' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

log() { echo "[worker-leak-guard] $*" >&2; }

# Orphan == reparented to init. Reading /proc directly avoids a `ps` call per
# pid and is the same check on every Linux; on non-Linux there is no /proc and
# the guard simply finds nothing (fail-safe: never kills on unknown state).
is_orphan() {
  local ppid
  ppid=$(awk '/^PPid:/{print $2}' "/proc/$1/status" 2>/dev/null) || return 1
  [ "${ppid:-0}" = "1" ]
}

age_seconds() {
  local secs
  secs=$(ps -p "$1" -o etimes= 2>/dev/null | tr -d ' ')
  [ -n "$secs" ] && echo "$secs" || echo 0
}

killed=0
scanned=0
min_secs=$((ORPHAN_MIN_MIN * 60))

for pat in $PATTERNS; do
  # -x: exact process NAME. Deliberately NOT `pgrep -f <pattern>`, which would
  # also match any shell/editor whose command line merely mentions the name.
  while IFS= read -r pid; do
    [ -n "$pid" ] || continue
    scanned=$((scanned + 1))
    is_orphan "$pid" || continue
    age=$(age_seconds "$pid")
    [ "$age" -ge "$min_secs" ] || continue
    rss=$(ps -p "$pid" -o rss= 2>/dev/null | tr -d ' ')
    if [ "$DRY_RUN" = 1 ]; then
      log "DRY-RUN would kill $pat pid=$pid age=${age}s rss=$(( ${rss:-0} / 1024 ))MB"
    else
      log "kill $pat pid=$pid age=${age}s rss=$(( ${rss:-0} / 1024 ))MB"
      # TERM first so a worker mid-write can flush; KILL only if it ignores us.
      kill -TERM "$pid" 2>/dev/null || true
      ( sleep 5; kill -0 "$pid" 2>/dev/null && kill -KILL "$pid" 2>/dev/null ) &
    fi
    killed=$((killed + 1))
  done < <(pgrep -x "$pat" 2>/dev/null || true)
done

wait 2>/dev/null || true
log "done scanned=$scanned ${DRY_RUN:+dry_run=1 }killed=$killed min_age=${ORPHAN_MIN_MIN}m patterns='$PATTERNS'"
exit 0
