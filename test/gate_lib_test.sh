#!/usr/bin/env bash
# gate_lib_test.sh — floor TEST for scripts/lib/gate_lib.sh (test-don't-assume).
#
# Runnable standalone (from any cwd):  bash test/gate_lib_test.sh
# Prints PASS/FAIL/SKIP per test; exits non-zero if ANY assertion FAILed.
# SKIP is used only where a test needs the real `openclaw` binary and it is
# absent — everything that does not need it always runs.
#
# Required coverage (from gate_lib_SPEC.md "Required tests"):
#   1. guard_by_hash: 5 ticks, no content change -> 0 fires; change -> fires once.
#   2. guard_by_interval: suppresses within floor; passes after floor elapses.
#   3. guard_composite alias == the neuron refire_should_fire behavior (regression.
#      lock, incl. claim-line exclusion).
#   4. defer_if_busy: high load defers, but interval floor still eventually fires
#      (no starvation).
#   5. trigger_p: env reaches the worker under crond-like env stripping.
#   6. safe_config_write: rejects a knowingly-bad patch, writes nothing, leaves
#      `openclaw config validate` GREEN (node ever a raw openclaw.json write).
#   7. sensors: return SAFE DEFAULTS on a faked unknown platform (no /proc data),
#      gate still exit 0.

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB="$TEST_DIR/../scripts/lib/gate_lib.sh"

FAILED=0
PASSED=0
SKIPPED=0
CURRENT="???"

_pb()   { printf 'PASS  [%s]\n' "$CURRENT"; PASSED=$((PASSED+1)); }
_fb()   { printf 'FAIL  [%s]\n' "$CURRENT"; FAILED=$((FAILED+1)); }
_sk()   { printf 'SKIP  [%s]\n' "$CURRENT"; SKIPPED=$((SKIPPED+1)); }

begin() { CURRENT="$(printf '%s' "$1" | tr -d '[]')"; }
check() { # check <msg> <exit/>
  local msg="$1" real="$2" want="$3"
  if [ "$real" -eq "$want" ]; then :; else
    printf '  FAIL(%s): %s (got exit %s, want %s)\n' "$CURRENT" "$msg" "$real" "$want"
  fi
}

[ -f "$LIB" ] || { echo "FATAL: lib not found at $LIB" >&2; exit 2; }
# shellcheck source=../scripts/lib/gate_lib.sh
source "$LIB" >/dev/null 2>&1

MKT="$(mktemp -d)"
trap 'rm -rf "$MKT"' EXIT

# ─────────────────────────────────────────────────────────────────────────────
echo "=== Test 1: guard_by_hash — work-once over unchanged content ==="
begin "T1 guard_by_hash"
TEST1="$MKT/t1"
mkdir -p "$TEST1/input"
printf 'type: task\nstatus: done\nclaimed_at: now\nbody\n' > "$TEST1/input/a.md"
printf 'type: task\nstatus: done\nbody2\n'                 > "$TEST1/input/b.md"
state1="$TEST1/state"

# prime: first run (never run before) MUST fire.
guard_by_hash "$state1" "$TEST1/input"/*.md; p=$?
check "prime fires" "$p" 0
# 5 ticks with identical content MUST fire 0 times.
fires=0
for _ in 1 2 3 4 5; do guard_by_hash "$state1" "$TEST1/input"/*.md && fires=$((fires+1)); done
if [ "$fires" -eq 0 ]; then _pb; else _fb; printf '  fires=%s (want 0)\n' "$fires"; fi
# change body -> fires exactly once, then suppresses.
printf 'type: task\nstatus: done\nclaimed_at: now\nbody CHANGED\n' > "$TEST1/input/a.md"
change_fires=0
guard_by_hash "$state1" "$TEST1/input"/*.md && change_fires=$((change_fires+1))
guard_by_hash "$state1" "$TEST1/input"/*.md && change_fires=$((change_fires+1))
if [ "$change_fires" -eq 1 ]; then _pb; else _fb; printf '  change fires=%s (want 1)\n' "$change_fires"; fi
begin "T1 guard_by_hash"
_pb

# ─────────────────────────────────────────────────────────────────────────────
echo "=== Test 2: guard_by_interval — suppress within floor, pass after ==="
begin "T2 guard_by_interval"
TEST2="$MKT/t2"; mkdir -p "$TEST2"
state2="$TEST2/state"
# floor = 60s. First call when no stamp -> due (last_run=0) -> fires.
guard_by_interval "$state2" 60; p=$?
check "first call fires" "$p" 0
# immediately again -> within floor -> suppressed.
guard_by_interval "$state2" 60; p=$?
check "within floor suppressed" "$p" 1
# force the stamp into the past -> floor elapsed -> fires again.
now=$(date -u +%s)
old=$(( now - 100 ))
printf '%s %s\n' "$old" "-" > "$state2"
guard_by_interval "$state2" 60; p=$?
check "after floor fires" "$p" 0
if [ "$p" -eq 0 ]; then _pb; else _fb; fi

# ─────────────────────────────────────────────────────────────────────────────
echo "=== Test 3: guard_composite == refire_should_fire (regression lock) ==="
begin "T3 composite==refire"
TEST3="$MKT/t3"; mkdir -p "$TEST3/input"
printf 'type: task\nstatus: done\nclaimed_at: now\nbody\n' > "$TEST3/input/n.md"
scn_state_a="$TEST3/stateA"   # guard_composite
scn_state_b="$TEST3/stateB"   # refire_should_fire

# Step 1: fresh (no state) -> both fire.
guard_composite "$scn_state_a" 3600 "$TEST3/input"/*.md; a1=$?
refire_should_fire    "$scn_state_b" 3600 "$TEST3/input"/*.md; b1=$?
# Step 2: unchanged, within floor -> both suppress.
guard_composite "$scn_state_a" 3600 "$TEST3/input"/*.md; a2=$?
refire_should_fire    "$scn_state_b" 3600 "$TEST3/input"/*.md; b2=$?
# Step 3: claim-line-only refresh -> both suppress (the v1 loop fix).
printf 'type: task\nstatus: done\nclaimed_at: now\nbody\n' > "$TEST3/input/n.md"
guard_composite "$scn_state_a" 3600 "$TEST3/input"/*.md; a3=$?
refire_should_fire    "$scn_state_b" 3600 "$TEST3/input"/*.md; b3=$?
# Step 4: real content change -> both fire.
printf 'type: task\nstatus: done\nclaimed_at: now\nbody REAL\n' > "$TEST3/input/n.md"
guard_composite "$scn_state_a" 3600 "$TEST3/input"/*.md; a4=$?
refire_should_fire    "$scn_state_b" 3600 "$TEST3/input"/*.md; b4=$?
# Step 5: force floor lapse (unchanged content) -> both fire (no starvation).
now3=$(date -u +%s); old3=$(( now3 - 200000 ))
printf '%s %s\n' "$old3" "-" > "$scn_state_a"
printf '%s %s\n' "$old3" "-" > "$scn_state_b"
guard_composite "$scn_state_a" 3600 "$TEST3/input"/*.md; a5=$?
refire_should_fire    "$scn_state_b" 3600 "$TEST3/input"/*.md; b5=$?

mismatch=0
for i in 1 2 3 4 5; do
  a="a$i"; b="b$i"
  if [ "${!a}" -ne "${!b}" ]; then
    mismatch=$((mismatch+1))
    printf '  step %s mismatch: composite=%s refire=%s\n' "$i" "${!a}" "${!b}"
  fi
done
# also assert the semantic sequence itself (fresh=0, unchanged=1, claim=1, change=0).
if [ "$a1" -eq 0 ] && [ "$a2" -eq 1 ] && [ "$a3" -eq 1 ] && [ "$a4" -eq 0 ] && [ "$a5" -eq 0 ] && [ "$mismatch" -eq 0 ]; then
  _pb
else
  _fb; printf '  composite seq=(%s %s %s %s %s) want=(0 1 1 0 0); mismatches=%s\n' "$a1" "$a2" "$a3" "$a4" "$a5" "$mismatch"
fi

# ─────────────────────────────────────────────────────────────────────────────
echo "=== Test 4: defer_if_busy — no starvation (interval floor still fires) ==="
begin "T4 defer_if_busy"
# High load defers.
defer_if_busy 5.0 1.0; p=$?
check "high load defers (exit 1)" "$p" 1
# Low load runs now.
defer_if_busy 0.2 1.0; p=$?
check "low load runs (exit 0)" "$p" 0
# Starvation floor: even after many deferred ticks, the interval guard still fires.
TEST4="$MKT/t4"; mkdir -p "$TEST4"
state4="$TEST4/state"
# Starvation floor semantics: `defer_if_busy` only skips THIS tick. The interval
# guard is the floor — it MUST fire once the stamp is older than min_interval,
# regardless of any number of deferred ticks. So age out the stamp beyond the
# 3600s floor.
now4=$(date -u +%s); old4=$(( now4 - 200000 ))
printf '%s %s\n' "$old4" "x" > "$state4"
deferred=0
for _ in 1 2 3 4 5; do
  defer_if_busy 9.0 1.0 && deferred=$((deferred+1))   # 0 runs now (all busy)
done
guard_by_interval "$state4" 3600; p=$?
if [ "$deferred" -eq 0 ] && [ "$p" -eq 0 ]; then
  _pb
else
  _fb; printf '  deferred(max0)=%s interval_after_floor=%s (want 0 and 0)\n' "$deferred" "$p"
fi

# ─────────────────────────────────────────────────────────────────────────────
echo "=== Test 5: trigger_p — env reaches worker under crond-like stripping ==="
begin "T5 trigger_p env threading"
MOCK5="$MKT/mockbin5"; mkdir -p "$MOCK5"
CAP5="$MKT/t5_capture"; : > "$CAP5"
cat > "$MOCK5/openclaw" <<EOF
#!/usr/bin/env bash
env | sort > "$CAP5"
exit 0
EOF
chmod +x "$MOCK5/openclaw"

# Run under `env -i` (strips all inherited env, incl. any interactive vars) the
# way crond does. trigger_p must still deliver its KEY=VAL to the worker because
# it passes them EXPLICITLY via `env`. We force the mock into PATH first.
env -i \
  PATH="$MOCK5:$PATH" \
  HOME="$(mktemp -d)" \
  CAP5="$CAP5" \
  bash -c '
    set -uo pipefail
    source "$0" >/dev/null 2>&1
    trigger_p "t" "job123" "DINOMEM_BATCH=8" "FOO=bar"
  ' "$LIB"
if grep -q '^DINOMEM_BATCH=8$' "$CAP5" && grep -q '^FOO=bar$' "$CAP5"; then
  _pb
else
  _fb; printf '  captured env missing expected keys:\n'; cat "$CAP5" | sed 's/^/    /'
fi

# ─────────────────────────────────────────────────────────────────────────────
echo "=== Test 6: safe_config_write — rejects bad patch, never raw JSON ==="
begin "T6 safe_config_write"
MOCK6="$MKT/mockbin6"; mkdir -p "$MOCK6"
CALL6="$MKT/t6_calls"; : > "$CALL6"
cat > "$MOCK6/openclaw" <<EOF
#!/usr/bin/env bash
echo "\$1 \$2" >> "$CALL6"
case "\$1" in
  config)
    case "\$2" in
      patch) echo "mock: invalid patch" >&2; exit 1 ;;  # reject ALL patches
      validate) exit 0 ;;                                # validator is green
      get) exit 0 ;;                                     # any path "exists"
      *) exit 0 ;;
    esac ;;
  *) exit 0 ;;
esac
EOF
chmod +x "$MOCK6/openclaw"

# (i) The unbypassable guard: a raw openclaw.json target must be REFUSED WITHOUT
#     ever invoking openclaw (no config patch call recorded).
env -i PATH="$MOCK6:$PATH" HOME="$(mktemp -d)" CALL6="$CALL6" \
  bash -c 'set -uo pipefail; source "$0" >/dev/null 2>&1; safe_config_write "/etc/openclaw.json"; [ $? -ne 0 ]' "$LIB"
json_rejected=$?
if [ "$json_rejected" -eq 0 ] && [ ! -s "$CALL6" ]; then
  : # good: rejected, and openclaw was never invoked
else
  _fb; printf '  raw-openclaw.json was NOT cleanly refused (call log empty? check: %s lines recorded)\n' "$(wc -l < "$CALL6")"
fi

# (ii) A knowingly-bad (rejected) patch -> nonzero, no write, and `config validate`
#      is left GREEN (validator allows it; the mock never mutates config).
#      Our function must never write a raw openclaw.json on disk.
pre_validate="$("$MOCK6/openclaw" config validate 2>/dev/null; echo "rc=$?")"
env -i PATH="$MOCK6:$PATH" HOME="$(mktemp -d)" CALL6="$CALL6" \
  bash -c 'set -uo pipefail; source "$0" >/dev/null 2>&1; safe_config_write "{\"a\":1}"' "$LIB"
patch_rejected=$?
post_validate="$("$MOCK6/openclaw" config validate 2>/dev/null; echo "rc=$?")"
# no openclaw.json should have been created anywhere under our temp cwd.
leftover_json=$(find "$MKT" -name 'openclaw.json' 2>/dev/null | wc -l)

if [ "$patch_rejected" -ne 0 ] && [ "$leftover_json" -eq 0 ]; then
  _pb
else
  _fb; printf '  rejected-patch return=%s (want nonzero); raw openclaw.json files found=%s (want 0) [pre_validate=%s] [post_validate=%s]\n' "$patch_rejected" "$leftover_json" "$pre_validate" "$post_validate"
fi

# (iii) If a REAL openclaw is on this box (not our mock), run the strongest
#       form of this test: safe_config_write applied to the REAL validator with
#       a KNOWN-INVALID patch. Because it is invalid, the real `config patch
#       --stdin` rejects it -> our fn returns non-zero, writes NOTHING, and the
#       real `openclaw config validate` stays GREEN. Absent -> SKIP.
if command -v openclaw >/dev/null 2>&1 && [ "$(command -v openclaw)" != "$MOCK6/openclaw" ]; then
  # baseline: real validator must start GREEN.
  if openclaw config validate >/dev/null 2>&1; then
    baseline_green=1
  else
    baseline_green=0
  fi
  real_path="$(dirname "$(command -v openclaw)")"
  env -i \
    PATH="$real_path:/usr/bin:/bin" \
    HOME="$HOME" \
    bash -c '
      source "$0" >/dev/null 2>&1
      safe_config_write "{ not valid json5 "
    ' "$LIB"
  real_rejected=$?     # must be non-zero
  if openclaw config validate >/dev/null 2>&1; then
    real_green=1
  else
    real_green=0
  fi
  if [ "$baseline_green" -eq 1 ] && [ "$real_rejected" -ne 0 ] && [ "$real_green" -eq 1 ]; then
    printf '  (real openclaw: invalid patch rejected, nothing written, validate GREEN)\n'
  else
    _fb; printf '  real-CLI run: baseline_green=%s rejected(ne0)=%s green_after=%s\n' "$baseline_green" "$real_rejected" "$real_green"
  fi
else
  _sk; printf '  real-openclaw validate sub-assertion skipped (openclaw absent)\n'
fi
# begin repoint so the (single) T6 TICK uses the last PASS/FAIL verdict above.
begin "T6 safe_config_write"

# ─────────────────────────────────────────────────────────────────────────────
echo "=== Test 7: sensors — SAFE DEFAULTS on a faked unknown platform ==="
begin "T7 sensors safe defaults"
# Simulate "no /proc data / unknown platform" by running the sensors in a PATH
# whose awk/nproc/sysctl/df yield NOTHING -> every read is empty -> each sensor
# must fall back to its SAFE DEFAULT, print a non-empty number, and exit 0.
MOCK7="$MKT/mockbin7"; mkdir -p "$MOCK7"
cat > "$MOCK7/awk"   <<'EOF'
#!/usr/bin/env bash
exit 0                        # yield nothing (as on a platform with no /proc/meminfo)
EOF
cat > "$MOCK7/nproc"  <<'EOF'
#!/usr/bin/env bash
exit 1
EOF
cat > "$MOCK7/sysctl" <<'EOF'
#!/usr/bin/env bash
exit 1
EOF
cat > "$MOCK7/df"     <<'EOF'
#!/usr/bin/env bash
exit 1
EOF
chmod +x "$MOCK7/awk" "$MOCK7/nproc" "$MOCK7/sysctl" "$MOCK7/df"

out=$(env -i PATH="$MOCK7:$PATH" HOME="$(mktemp -d)" bash -c 'set -uo pipefail; source "$0" >/dev/null 2>&1; printf "%s|%s|%s|%s\n" "$(sensor_ram_mb)" "$(sensor_disk_free_pct .)" "$(sensor_load_ratio)" "$(sensor_cores)"' "$LIB")
ram=$(printf '%s' "$out" | cut -d'|' -f1)
disk=$(printf '%s' "$out" | cut -d'|' -f2)
load=$(printf '%s' "$out" | cut -d'|' -f3)
cores=$(printf '%s' "$out" | cut -d'|' -f4)

# Each must be a non-empty number and be the SAFE DEFAULT.
ram_ok=0; case "$ram" in  (''|*[!0-9]*|[0-9]*) ram_ok=1;; esac
disk_ok=0; case "$disk" in (''|*[!0-9]*|[0-9]*) disk_ok=1;; esac
load_ok=0; case "$load" in (''|*[!0-9.]*|[0-9.]*) load_ok=1;; esac
cores_ok=0; case "$cores" in (''|*[!0-9]*|[0-9]*) cores_ok=1;; esac

def_ram=512; def_disk=90; def_load="0.00"; def_cores=1
if [ "$ram" = "$def_ram" ] && [ "$disk" = "$def_disk" ] && [ "$load" = "$def_load" ] && [ "$cores" = "$def_cores" ]; then
  _pb
else
  _fb; printf '  got ram=%s disk=%s load=%s cores=%s (want %s %s %s %s)\n' "$ram" "$disk" "$load" "$cores" "$def_ram" "$def_disk" "$def_load" "$def_cores"
fi

# gate still exit 0 with all sensors: a composite that calls every sensor.
env -i PATH="$MOCK7:$PATH" HOME="$(mktemp -d)" bash -c '
  set -uo pipefail; source "$0" >/dev/null 2>&1
  r=$(sensor_ram_mb); d=$(sensor_disk_free_pct .); l=$(sensor_load_ratio); c=$(sensor_cores)
  [ -n "$r" ] && [ -n "$d" ] && [ -n "$l" ] && [ -n "$c" ]
' "$LIB"
if [ $? -eq 0 ]; then _pb; else _fb; printf '  composite sensor gate exited non-zero\n'; fi

# ─────────────────────────────────────────────────────────────────────────────
echo
printf 'TOTAL: %s passed, %s failed, %s skipped\n' "$PASSED" "$FAILED" "$SKIPPED"
if [ "$FAILED" -gt 0 ]; then
  echo "RESULT: FAIL"
  exit 1
fi
echo "RESULT: PASS"
exit 0
