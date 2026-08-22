#!/usr/bin/env bash
# gate_lib.sh — efficiency + safety primitives for the cron gate harness.  [v1]
#
# SOURCEABLE bash library, NOT executable. Sourcel it from any zero-LLM check
# script or gate cron:
#   source "$SCRIPTS/lib/gate_lib.sh"
#
# PURPOSE (the frame): the cron gate (`cron_gate.sh`) is a harness dinomem hands
# to the installing user's agent so whatever THAT agent builds comes out
# cost-efficient by default — a cheap zero-LLM check decides, the expensive LLM
# worker wakes only on a real signal. This lib extends that harness from
# cost-only to efficient + safe across every resource axis.
#
# LAYERS:
#   A  waste-floor  : guard_by_hash / guard_by_interval / guard_composite
#   B  sensors      : sensor_ram_mb / sensor_disk_free_pct / sensor_load_ratio
#                     / sensor_cores  (read-only, adaptive input)
#   C  trigger      : trigger / trigger_p / pick_batch / defer_if_busy
#   D  safety-floor : safe_config_write / schema_field_ok / register_worker /
#                     docs_hint  (version-matched, offline-proof, unbypassable)
#
# CROSS-CUTTING CONTRACTS (every function):
#   - fail-open: internal error -> safe default; the gate tick still exits 0.
#     A broken primitive can NEVER brick a tick.
#   - zero-LLM: Layers A-B-C never call a model. Only trigger* wakes the paid
#     worker.
#   - versioned: each fn carries `# vN`; better default -> name_v2, old kept.
#   - offline + version-matched for Layer D: bound to the LOCAL installed
#     validator, never to "latest" docs.
#   - set -uo pipefail friendly: functions RETURN, don't exit, so a sourced fn
#     cannot kill a gate that runs under `set -e`.
#
# BUG HISTORY (do not re-ship): v0 fired on bare note EXISTENCE -> LLM woke
# 96x/day with NO_REPLY. v1 gated on note MTIME vs a stamp -> SELF-PERPETUATING
# LOOP because claim refreshes bump mtime after the stamp. Fix: hash the BODY,
# EXCLUDING the volatile claim lines (their exclusion IS the v1 loop fix). Every
# floor below is backed by test/gate_lib_test.sh, not by assertion.

set -uo pipefail 2>/dev/null || true

# ─────────────────────────────────────────────────────────────────────────────
# Layer A — waste-floor (work-once + debounce).  [v1]
# ─────────────────────────────────────────────────────────────────────────────

# body hash of one input EXCLUDING volatile claim lines (claimed_by:/claimed_at:).
# A pure claim refresh leaves the hash unchanged -> the guard stays quiet; a real
# edit changes it -> the guard fires. This exclusion is the v1 loop fix.
gate__body_hash() {
  # $1 = file path
  grep -vE '^claimed_(by|at):' "$1" 2>/dev/null | sha256sum 2>/dev/null | cut -d' ' -f1
}

# aggregate content hash over all inputs (order-stable via sort).
gate__agg_hash() {
  # "$@" = note/input file paths
  local f
  for f in "$@"; do
    printf '%s %s\n' "$(basename "$f")" "$(gate__body_hash "$f")"
  done | sort | sha256sum 2>/dev/null | cut -d' ' -f1
}

# guard_by_hash <state_file> <input...>     # v1  -> 0 fire / 1 skip
# Exit 0 (run) iff aggregate content-hash of <input...> differs from state_file;
# else exit 1 (skip). Hash EXCLUDES volatile claim lines. Stamps new hash on run.
# If no inputs were passed, echoes no work and returns 1 (nothing to change).
guard_by_hash() {
  local state_file="$1"; shift
  local -a inputs=("$@")
  [ "${#inputs[@]}" -gt 0 ] || return 1

  local agg_hash; agg_hash=$(gate__agg_hash "${inputs[@]}")
  [ -n "$agg_hash" ] || return 1   # fail-open: no hash -> no reliable change -> skip

  local now_epoch; now_epoch=$(date -u +%s 2>/dev/null || echo 0)
  local last_hash=""
  if [ -f "$state_file" ]; then
    read -r _ last_hash < "$state_file" 2>/dev/null || true
  fi

  # never run before, or content changed -> fire + stamp fresh state.
  if [ -z "$last_hash" ] || [ "$agg_hash" != "$last_hash" ]; then
    printf '%s %s\n' "$now_epoch" "$agg_hash" > "$state_file" 2>/dev/null || true
    return 0
  fi
  return 1
}

# guard_by_interval <state_file> <min_secs>     # v1  -> 0 fire / 1 skip
# Exit 0 iff now - last_run >= min_secs; else exit 1. Stamps on run.
guard_by_interval() {
  local state_file="$1" min_interval="$2"
  local now_epoch; now_epoch=$(date -u +%s 2>/dev/null || echo 0)

  local last_run=0
  if [ -f "$state_file" ]; then
    read -r last_run _ < "$state_file" 2>/dev/null || true
    case "$last_run" in (''|*[!0-9]*) last_run=0 ;; esac
  fi

  local age=$(( now_epoch - last_run ))
  if [ "$age" -ge "$min_interval" ]; then
    printf '%s %s\n' "$now_epoch" "-" > "$state_file" 2>/dev/null || true
    return 0
  fi
  return 1
}

# gate__crash_signature <note...>     # 0 if ANY note looks like a crashed worker
# CRASH SIGNATURE: a project note that is in_progress with a STALE live-session-*
# claim on an UNFINISHED step = a live worker that died mid-step and never
# released. This is distinct from a PAUSED-at-safety-gate note (Advancer released
# the claim -> no live-session claim) which must STAY throttled. Detecting a crash
# lets the composite guard fire the Advancer IMMEDIATELY (bypass the daily floor)
# instead of waiting up to min_interval to resume dropped work.
#   stale = claimed_at older than GATE_CRASH_STALE_SECS (default 1800 = 30min,
#           matching claim_note.sh LIVE_WINDOW_MIN so writer+guard agree).
# Fail-CLOSED for the crash path only (unparseable/missing -> NOT a crash -> no
# early fire), so a bad parse can never spuriously wake the LLM; the normal
# hash/interval terms still decide. Frontmatter parse matches claim_note.sh:
# bare `key: value` block ending at the first blank line (also tolerates a
# leading `---` fenced block).
gate__crash_signature() {
  local now_epoch; now_epoch=$(date -u +%s 2>/dev/null || echo 0)
  [ "$now_epoch" -gt 0 ] || return 1   # no clock -> can't age -> not a crash
  local stale_secs="${GATE_CRASH_STALE_SECS:-1800}"
  local f
  for f in "$@"; do
    [ -f "$f" ] || continue
    # pull frontmatter fields (first block only; stop at blank line).
    local fm status cur_by cur_at
    fm=$(awk '
      NR==1 && $0=="---" { fenced=1; infm=1; next }
      NR==1 && /^#/       { infm=1; next }
      NR==1              { exit }
      infm && fenced && $0=="---" { exit }
      infm && !fenced && NF==0    { exit }
      infm { print }
    ' "$f" 2>/dev/null)
    status=$(printf '%s\n' "$fm"   | grep -m1 '^status:'     | sed 's/^status:[[:space:]]*//')
    cur_by=$(printf '%s\n' "$fm"   | grep -m1 '^claimed_by:' | sed 's/^claimed_by:[[:space:]]*//')
    cur_at=$(printf '%s\n' "$fm"   | grep -m1 '^claimed_at:' | sed 's/^claimed_at:[[:space:]]*//')
    # must be an in_progress note, held by a live-session-* claimant.
    [ "$status" = "in_progress" ] || continue
    case "$cur_by" in live-session-*) : ;; *) continue ;; esac
    [ -n "$cur_at" ] || continue
    local ce; ce=$(date -u -d "$cur_at" +%s 2>/dev/null || echo "")
    [ -n "$ce" ] || continue                 # unparseable -> not a crash
    [ "$ce" -le "$now_epoch" ] || continue    # future-dated -> not a crash
    # STALE live claim on an in_progress note = crashed worker.
    if [ $(( now_epoch - ce )) -ge "$stale_secs" ]; then
      return 0
    fi
  done
  return 1
}

# guard_composite <state_file> <min_secs> <input...>     # v1+crash -> 0 fire / 1 skip
# = guard_by_hash AND guard_by_interval evaluated over ONE state file, PLUS an
# early-fire crash-detect term (added 2026-08-14): a crashed live worker's note
# (stale live-session claim on an unfinished in_progress step) fires immediately
# so dropped work resumes in ~one tick instead of waiting the daily floor.
# This IS today's refire_should_fire(); kept name-compatible via backward-alias
# (guard_composite is the primitive; alias below for callers already sourcing
# the old neuron shim). Non-crash behavior identical to the proven v1 refire
# guard: fires iff (never-run OR content changed) OR (interval floor elapsed).
guard_composite() {
  local state_file="$1" min_interval="$2"; shift 2
  local -a inputs=("$@")

  # no qualifying input -> nothing to do. (Callers normally exit 1 themselves.)
  [ "${#inputs[@]}" -gt 0 ] || return 1

  local now_epoch; now_epoch=$(date -u +%s 2>/dev/null || echo 0)
  local agg_hash; agg_hash=$(gate__agg_hash "${inputs[@]}")
  [ -n "$agg_hash" ] || return 1   # fail-open

  # read prior "<epoch> <hash>" stamp.
  local last_run=0 last_hash=""
  if [ -f "$state_file" ]; then
    read -r last_run last_hash < "$state_file" 2>/dev/null || true
    case "$last_run" in (''|*[!0-9]*) last_run=0 ;; esac
  fi

  # (A) content changed since last real run? (never-run -> fires)
  local changed=1
  if [ "$last_run" -gt 0 ] && [ -n "$last_hash" ]; then
    [ "$agg_hash" != "$last_hash" ] && changed=0
  else
    changed=0
  fi

  # (b) min-interval floor elapsed?
  local age=$(( now_epoch - last_run ))
  local due=1
  [ "$age" -ge "$min_interval" ] && due=0

  # (c) crash-detect early-fire: a crashed live worker's note resumes NOW,
  #     bypassing the daily floor. Fail-closed (helper returns 1 on any doubt),
  #     so this can only ADD fires for genuine crashes, never suppress a normal one.
  local crashed=1
  gate__crash_signature "${inputs[@]}" && crashed=0

  if [ "$changed" -eq 0 ] || [ "$due" -eq 0 ] || [ "$crashed" -eq 0 ]; then
    printf '%s %s\n' "$now_epoch" "$agg_hash" > "$state_file" 2>/dev/null || true
    return 0
  fi
  return 1
}

# guard_composite_pernote <state_file> <min_secs> <input...>   # 0 fire / 1 skip
# PER-NOTE variant of guard_composite. Fixes the aggregate-hash STARVATION flaw:
# guard_composite keys one hash over the WHOLE input SET, so a busy note that
# keeps changing perpetually refreshes the shared stamp and a co-qualifying but
# quiet note (e.g. one parked between advances) can be starved of fires until the
# daily floor. This variant keeps ONE state line PER note (basename<TAB>epoch
# hash), so each note fires on ITS OWN (never-seen | body-changed | own-floor |
# crash) terms, independent of sibling churn.
#
# FIRES (exit 0) if ANY input qualifies on its own terms; stamps ONLY the
# note(s) that fired (quiet notes keep their prior timer -> their floor still
# elapses independently). Fail-open: unreadable state / no hash -> treat that
# note as never-seen (fire), never silently starve. Reuses gate__body_hash +
# gate__crash_signature (single source of truth, identical claim-line exclusion
# and crash semantics as guard_composite). Behaviour for a SINGLE input is
# identical to guard_composite (regression-locked in the test).
guard_composite_pernote() {
  local state_file="$1" min_interval="$2"; shift 2
  local -a inputs=("$@")
  [ "${#inputs[@]}" -gt 0 ] || return 1

  local now_epoch; now_epoch=$(date -u +%s 2>/dev/null || echo 0)

  # Load prior per-note stamps into an assoc map: key=basename -> "<epoch> <hash>".
  declare -A _pn_last_run _pn_last_hash
  if [ -f "$state_file" ]; then
    local _k _e _h
    while IFS=$'\t' read -r _k _e _h; do
      [ -n "$_k" ] || continue
      case "$_e" in (''|*[!0-9]*) _e=0 ;; esac
      _pn_last_run["$_k"]="$_e"
      _pn_last_hash["$_k"]="$_h"
    done < "$state_file"
  fi

  local any_fire=1   # 1 = none fired yet (bash: 0 = success/fire)
  local f base bhash lr lh changed due crashed
  for f in "${inputs[@]}"; do
    [ -f "$f" ] || continue
    base="$(basename "$f")"
    bhash="$(gate__body_hash "$f")"
    [ -n "$bhash" ] || bhash="-"          # fail-open: no hash -> forces a fire below
    lr="${_pn_last_run[$base]:-0}"
    lh="${_pn_last_hash[$base]:-}"

    # (A) never-seen OR body changed since this note's last real run.
    changed=1
    if [ "$lr" -gt 0 ] && [ -n "$lh" ]; then
      [ "$bhash" != "$lh" ] && changed=0
    else
      changed=0
    fi
    # (b) this note's OWN min-interval floor elapsed.
    due=1
    [ $(( now_epoch - lr )) -ge "$min_interval" ] && due=0
    # (c) crash-detect early-fire for THIS note (fail-closed).
    crashed=1
    gate__crash_signature "$f" && crashed=0

    if [ "$changed" -eq 0 ] || [ "$due" -eq 0 ] || [ "$crashed" -eq 0 ]; then
      # this note fires -> stamp it fresh.
      _pn_last_run["$base"]="$now_epoch"
      _pn_last_hash["$base"]="$bhash"
      any_fire=0
    fi
    # quiet note: leave its prior stamp untouched (timer keeps counting).
  done

  # Persist the full map (fired notes refreshed, quiet notes preserved). Prune
  # keys for inputs no longer present is implicit: we only re-write keys we saw
  # this run PLUS any prior key still in the map. To avoid unbounded growth from
  # deleted notes, only keep keys that were in THIS run's inputs.
  local -A _keep
  for f in "${inputs[@]}"; do _keep["$(basename "$f")"]=1; done
  {
    local k
    for k in "${!_pn_last_run[@]}"; do
      [ -n "${_keep[$k]:-}" ] || continue    # drop vanished notes (GC)
      printf '%s\t%s\t%s\n' "$k" "${_pn_last_run[$k]}" "${_pn_last_hash[$k]:-}"
    done
  } > "$state_file" 2>/dev/null || true

  return $any_fire
}

# Back-compat alias: the neuron guard name IS guard_composite. Kept so the thin
# shim (and any old caller) keeps working with zero churn.
if ! declare -F refire_should_fire >/dev/null 2>&1; then
  refire_should_fire() { guard_composite "$@"; }
fi

# ─────────────────────────────────────────────────────────────────────────────
# Layer B — resource sensors (adaptive input; READ-ONLY).  [v1]
#
# CONTRACT (all): pure stdout number, exit 0 ALWAYS, fail-open to a SAFE DEFAULT
# on unknown platform (BusyBox/macOS: no /proc) — never empty, never crash the
# gate. Sensors DECIDE NOTHING; the caller decides (see pick_batch/defer_if_busy).
# ─────────────────────────────────────────────────────────────────────────────

# sensor_ram_mb -> available MB (Linux /proc/meminfo; fallback sysctl on macOS).
# Safe default: 512 MB.
sensor_ram_mb() {
  if [ -r /proc/meminfo ]; then
    local avail kB
    avail=$(awk '/^MemAvailable:/{print $2}' /proc/meminfo 2>/dev/null | tr -d ' ')
    if [ -n "$avail" ]; then
      kB=${avail%%.*}
      case "$kB" in (''|*[!0-9]*) kB=0 ;; esac
      [ "$kB" -ge 1 ] && { echo "$(( kB / 1024 ))"; return 0; }
    fi
  fi
  # macOS / BusyBox fallback: sysctl hw.memsize? not free memory. Never empty.
  echo "512"   # SAFE DEFAULT
  return 0
}

# sensor_disk_free_pct <path> -> int % free on path's fs (df -P portable).
# Safe default: 90 (assume plenty free).
sensor_disk_free_pct() {
  local path="$1"
  [ -z "$path" ] && path="."
  local pct
  pct=$(df -P "$path" 2>/dev/null | awk 'NR==2{print $5}' | tr -d '%' )
  case "$pct" in (''|*[!0-9]*) pct=90 ;; esac
  echo "$pct"
  return 0
}

# sensor_load_ratio -> loadavg / nproc (1.0 = saturated). Linux /proc/loadavg.
# Safe default: 0 (idle).
sensor_load_ratio() {
  local load1 cores
  if [ -r /proc/loadavg ]; then
    load1=$(awk '{print $1}' /proc/loadavg 2>/dev/null | tr -d ' ')
    if [ -n "$load1" ]; then
      cores=$(sensor_cores)
      case "$cores" in (''|*[!0-9]*) cores=1 ;; esac
      [ "$cores" -ge 1 ] || cores=1
      # awk floats both sides; print with 2 decimals so it is never empty.
      awk -v l="$load1" -v c="$cores" 'BEGIN{if(c<=0)c=1; printf "%.2f", l/c; exit}'
      return 0
    fi
  fi
  echo "0.00"   # SAFE DEFAULT (idle)
  return 0
}

# sensor_cores -> usable core count. Linux nproc; macOS sysctl; safe default 1.
sensor_cores() {
  local n
  n=$(nproc 2>/dev/null) || n=""
  case "$n" in (''|*[!0-9]*)
    n=$(sysctl -n hw.ncpu 2>/dev/null | tr -d ' ') || n=""
  ;; esac
  case "$n" in (''|*[!0-9]*) n=1 ;; esac
  [ "$n" -ge 1 ] || n=1
  echo "$n"
  return 0
}

# ─────────────────────────────────────────────────────────────────────────────
# Layer C — adaptive trigger (decide HOW, not just WHETHER).  [v1]
# ─────────────────────────────────────────────────────────────────────────────

# locate the openclaw CLI once (cron command jobs may run under a stripped env).
_gate__openclaw() {
  command -v openclaw 2>/dev/null || echo ""
}

# trigger <label> <jobid>     # v1  (UNCHANGED from today — back-compat.)
# Fire a DISABLED worker via `openclaw cron run <jobid>`. Swallow failures.
# Record the label on stdout.. empty label/jobid -> no-op. Returns 0 always
# unless the caller expects a signal; a broken trigger must never abort a gate.
trigger() {
  local name="$1" jobid="$2"
  local bin; bin=$(_gate__openclaw)
  if [ -z "$jobid" ] || [ -z "$bin" ]; then
    return 0
  fi
  if "$bin" cron run "$jobid" >/dev/null 2>&1; then
    echo "$name"
  else
    echo "gate_lib: trigger failed for $name ($jobid)" >&2
  fi
  return 0
}

# trigger_p <label> <jobid> <KEY=VAL>...     # v1
# Like trigger, but threads KEY=VAL... as ENV into the worker so it adapts
# (batch size, parallelism). Back-compat: old lanes keep calling plain trigger.
# MUST be tested under crond-style env stripping (install.sh known footgun):
# we pass env explicitly so it survives the stripped interactive env.
trigger_p() {
  local name="$1" jobid="$2"; shift 2
  local bin; bin=$(_gate__openclaw)
  if [ -z "$jobid" ] || [ -z "$bin" ]; then
    return 0
  fi
  # Build a cleaned env list (KEY=VAL pairs only; skip junk/empty).
  local -a envargs=() kv
  for kv in "$@"; do
    case "$kv" in
      *=*) envargs+=("$kv") ;;
    esac
  done
  if [ "${#envargs[@]}" -gt 0 ]; then
    if env "${envargs[@]}" "$bin" cron run "$jobid" >/dev/null 2>&1; then
      echo "$name"
    else
      echo "gate_lib: trigger failed for $name ($jobid)" >&2
    fi
  else
    trigger "$name" "$jobid"     # no env -> identical to plain trigger
  fi
  return 0
}

# pick_batch <ram_mb> -> a batch size that fits this box (tiered, override-able).
# Ranks to the largest batch whose comfortable working set fits available RAM.
# No ram given -> conservative 1. Override with DINOMEM_BATCH_MAX at call site.
pick_batch() {
  local ram_mb="$1"
  case "$ram_mb" in (''|*[!0-9]*) ram_mb=$(sensor_ram_mb) ;; esac
  [ "$ram_mb" -ge 1 ] || ram_mb=512
  local b=1
  if   [ "$ram_mb" -ge 16384 ]; then b=64
  elif [ "$ram_mb" -ge 8192 ];  then b=32
  elif [ "$ram_mb" -ge 4096 ];  then b=16
  elif [ "$ram_mb" -ge 2048 ];  then b=8
  elif [ "$ram_mb" -ge 1024 ];  then b=4
  elif [ "$ram_mb" -ge 512 ];   then b=2
  fi
  local max="${DINOMEM_BATCH_MAX:-}"
  case "$max" in (''|*[!0-9]*) max="" ;; esac
  if [ -n "$max" ] && [ "$max" -ge 1 ] && [ "$b" -gt "$max" ]; then
    b="$max"     # clamp to operator ceiling
  fi
  echo "$b"
  return 0
}

# defer_if_busy [load_ratio] [ceiling]     # v2  -> 0 run now / 1 defer (skip tick)
# Exit 1 (skip THIS tick) if load > ceiling (CPU backpressure). CONTRACT: MUST
# NOT defer forever — a saturated box must still eventually run, so pair this
# with guard_by_interval as a STARVATION FLOOR. fail-open: unknown load -> run
# now (never let a broken sensor stall a real signal).
# v2: BOTH args optional. Bare `defer_if_busy` self-senses load via
# sensor_load_ratio and uses a 1.5x-cores default ceiling, so a caller doesn't
# have to wire the sensor. Robust under `set -u` (no unbound-var on bare call).
defer_if_busy() {
  local load_ratio="${1:-}" ceiling="${2:-}"
  [ -z "$load_ratio" ] && load_ratio="$(sensor_load_ratio 2>/dev/null || echo 0)"
  case "$load_ratio" in (''|*[!0-9.]*) load_ratio=0 ;; esac
  case "$ceiling" in (''|*[!0-9.]*) ceiling=1.5 ;; esac
  # load_ratio is a float (e.g. 0.83). Compare with awk to avoid bash float math.
  awk -v l="$load_ratio" -v c="$ceiling" 'BEGIN{ exit (l > c ? 0 : 1) }'
  local rc=$?
  [ "$rc" -eq 0 ] && return 1   # busy -> defer
  return 0                      # idle/enough headroom -> run now
}

# gate_fire_or_defer [ceiling]     # v1  -> 0 fire now / 1 defer this tick
# The ONE backpressure decision every gate shares, factored out of the gates so
# they don't each copy-paste the declare-F guard + sensor + ceiling plumbing.
# Call it AFTER the gate has already decided it WOULD fire (refire guard passed
# and STATE_FILE stamped) as the final check. Self-senses load. STARVATION-SAFE
# by contract: caller must have stamped its interval STATE_FILE first, so the
# daily floor still elapses no matter how many ticks defer. fail-open: if the
# sensor is unknown, defer_if_busy returns 0 (headroom) -> we FIRE (never block).
#
# CONTRACT of the delegate (empirically pinned, do not "simplify" the polarity):
#   defer_if_busy  rc 0 = headroom/UNKNOWN -> FIRE ;  rc 1 = over ceiling -> DEFER.
# So this wrapper is a passthrough of that exit status: rc 0 fire, rc 1 defer.
gate_fire_or_defer() {
  defer_if_busy "" "${1:-2.0}"   # rc 0 = fire (headroom/unknown), rc 1 = defer (busy)
}

# ─────────────────────────────────────────────────────────────────────────────
# Layer D — safety-floor (version-matched, offline-proof, UNBYPASSABLE).  [v1]
# ─────────────────────────────────────────────────────────────────────────────

# safe_config_write <json5_patch>     # v1
# Writes config ONLY via the installed binary's validated path. On the real
# CLI that is `openclaw config patch --stdin` (JSON5 pipe) or `config set`.
# REFUSES raw openclaw.json edits — unbypassable. Invalid -> no write, non-zero,
# reason to stderr. Runs `openclaw config validate` after. Bound to the LOCAL
# installed validator => version-matched by construction, works offline.
# THE ONE PRIMITIVE THE AGENT MAY NOT REPLACE.
safe_config_write() {
  local patch="$1"
  local bin; bin=$(_gate__openclaw)

  # Unbypassable: NEVER a raw JSON file write.
  case "$patch" in
    openclaw.json|*[.]json)  echo "gate_lib: safe_config_write REFUSES raw openclaw.json edits" >&2; return 1 ;;
    --stdin|--file|-*)       echo "gate_lib: safe_config_write takes a JSON5 patch value only" >&2; return 1 ;;
  esac
  [ -n "$bin" ] || { echo "gate_lib: openclaw not found; cannot write config" >&2; return 1; }
  [ -n "$patch" ] || { echo "gate_lib: empty patch" >&2; return 1; }

  # Apply through the validated CLI path ONLY (`config patch --stdin`).
  if ! printf '%s\n' "$patch" | "$bin" config patch --stdin >/dev/null 2>&1; then
    echo "gate_lib: config patch rejected (invalid)" >&2
    return 1
  fi
  # Validate after the write; a regression must be surfaced, not swallowed.
  if ! "$bin" config validate >/dev/null 2>&1; then
    echo "gate_lib: config validate FAILED after patch — config left inconsistent" >&2
    return 1
  fi
  return 0
}

# schema_field_ok <dotted.path>     # v1 -> 0 known / 1 unknown-or-absent
# Confirm a field exists for THIS install BEFORE use, via the LOCAL validator.
# Prefers the specced `config.schema.lookup <path>`; if that subcommand is not
# present on this binary (2026.6.x+ exposes `config get <path>` instead), falls
# back to it. Both are local + version-matched + offline. Never trusts "latest"
# docs. Safe direction: if the local validator cannot confirm the field, return
# 1 (not-ok) so the caller won't bless a write to an unverified path. This is a
# pre-write SAFETY check, not a gate — it never aborts a tick.
schema_field_ok() {
  local path="$1"
  local bin; bin=$(_gate__openclaw)
  [ -n "$bin" ] || return 1
  [ -n "$path" ] || return 1

  if "$bin" config.schema.lookup "$path" >/dev/null 2>&1; then
    return 0
  fi
  # subcommand absent -> try the real CLI's local existence check (`config get`).
  if "$bin" config get "$path" >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

# register_worker <name> <schedule|disabled> <payload>     # v1
# Register/update a cron via MERGE-NOT-CLOBBER, agent-scoped. Mirrors install.sh
# upsert logic against the real `openclaw cron add` flag surface (2026.6.6+):
# `--name`, agent-scope `--agent`, exactly one schedule flag (--cron/--every/
# --at), `--disabled` when disabled, `--command`/`--message` from payload.
# If a job with our fully-qualified tag already exists, does NOTHING (never
# rewrites another agent's crontab lines). Fail-open: return 0 always; a failed
# register is logged to stderr, never fatal.
register_worker() {
  local name="$1" sched="$2" payload="$3"
  local bin; bin=$(_gate__openclaw)
  [ -n "$bin" ] || return 0
  [ -n "$name" ] || return 0
  [ -n "$sched" ] || sched=disabled

  local agent="${AGENT_ID:-default}"
  local tag="gate-${name}-${agent}"           # fully-qualified, agent-scoped

  # MERGE-NOT-CLOBBER: if OUR job already exists, leave it alone.
  if "$bin" cron list --all --json 2>/dev/null | grep -qE "\"name\"[[:space:]]*:[[:space:]]*\"${tag}\"|${tag}" 2>/dev/null; then
    return 0
  fi

  local -a reg=( "$bin" cron add --name "$tag" --agent "$agent" )
  # schedule: exactly one flag (or --disabled).
  if [ "$sched" = "disabled" ]; then
    reg+=( --disabled )
  elif [[ "$sched" == cron:* ]]; then
    reg+=( --cron "${sched#cron:}" )
  elif [[ "$sched" == every:* ]]; then
    reg+=( --every "${sched#every:}" )
  elif [[ "$sched" == at:* ]]; then
    reg+=( --at "${sched#at:}" )
  else
    echo "gate_lib: register_worker: malformed schedule '$sched' for $name (want disabled|cron:<expr>|every:<n>|at:<t>)" >&2
    return 0
  fi
  reg+=( --json )
  if ! "${reg[@]}" >/dev/null 2>&1; then
    echo "gate_lib: register_worker: openclaw cron add failed for $name" >&2
  fi
  return 0
}

# docs_hint <topic>     # v1 -> stdout advice (possibly empty); EXIT 0 ALWAYS.
# ENRICHMENT ONLY — never a gate. Version-pinned docs guidance; total failure ->
# empty string, caller proceeds. Output is advice, never authority.
docs_hint() {
  local topic="$1"
  local bin; bin=$(_gate__openclaw)
  [ -n "$bin" ] || { echo ""; return 0; }

  # Version-pinned: ask for the docs matching THIS install (not "latest").
  local v
  v=$("$bin" --version 2>/dev/null | head -n1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -n1) || v=""
  local out=""
  if [ -n "$v" ]; then
    out=$("$bin" help docs "$topic" --version "$v" 2>/dev/null) || out=""
  else
    out=$("$bin" help docs "$topic" 2>/dev/null) || out=""
  fi
  if [ -z "$out" ]; then
    # local doc bundle fallback under the workspace (best-effort).
    local bundle="${OPENCLAW_WORKSPACE:-$HOME/.openclaw/workspace}/docs/${topic}.md"
    if [ -f "$bundle" ]; then
      out=$(head -n 40 "$bundle" 2>/dev/null) || out=""
    fi
  fi
  printf '%s\n' "$out"
  return 0
}
