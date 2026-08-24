#!/usr/bin/env bash
# stop_heuristic_test.sh — pins scripts/stop-heuristic.sh loop-judgment contract. [v1]
#
# WHY: stop-heuristic decides whether the agent should keep fixing, widen
# inspection, stop, or distrust a shallow green. If that drifts, the autonomy
# loop either churns too long or declares victory too early.

set -uo pipefail 2>/dev/null || true
HERE="$(cd "$(dirname "$0")" && pwd)"
HINT="$HERE/../scripts/stop-heuristic.sh"

pass=*** fail=0
_run() { out="$(bash "$HINT" "$1" "$2" 2>&1)"; rc=$?; last="$(printf '%s\n' "$out" | tail -1)"; }
_ck() {
  local label="$1" want="$2" ex="$3"
  if printf '%s' "$last" | grep -Fq "$want" && [ "$rc" -eq "$ex" ]; then
    echo "  ok   $label -> $last (exit=$rc)"; pass=***
  else
    echo "  FAIL $label -> want [$want exit=$ex] got [$last exit=$rc]"; fail=$((fail+1))
  fi
}

_run 1 'VERIFY: FAIL bad.py (py_compile) :: SyntaxError; REPAIR_HINT: SYNTAX :: fix parser'
_ck 'early actionable red' 'STOP_HEURISTIC: KEEP_GOING ::' 0

_run 3 'VERIFY: FAIL bad.py (py_compile) :: SyntaxError; REPAIR_HINT: UNKNOWN :: inspect raw output manually'
_ck 'third try unclear red escalates' 'STOP_HEURISTIC: ESCALATE ::' 0

_run 5 'VERIFY: FAIL bad.py (py_compile) :: SyntaxError; REPAIR_HINT: SYNTAX :: fix parser'
_ck 'fifth try red stops' 'STOP_HEURISTIC: STOP ::' 0

_run 2 'VERIFY: PASS scripts/install.sh (bash_n); TEST_TARGET: NONE; DEPENDENCY_CHECK: NONE'
_ck 'shallow green is weak' 'STOP_HEURISTIC: WEAK_GREEN ::' 0

_run 2 'VERIFY: PASS scripts/verify.sh (py_compile); TEST_TARGET: bash test/verify_test.sh'
_ck 'strong green keeps sequence moving' 'STOP_HEURISTIC: KEEP_GOING ::' 0

_run 2 'BLAST_RADIUS: STOP_AND_INSPECT :: shared path with direct proof only'
_ck 'broad scope escalates' 'STOP_HEURISTIC: ESCALATE ::' 0

out="$(printf 'VERIFY: FAIL bad.py (py_compile) :: SyntaxError' | bash "$HINT" 1 2>&1)"; rc=$?; last="$(printf '%s\n' "$out" | tail -1)"
_ck 'stdin input' 'STOP_HEURISTIC: KEEP_GOING ::' 0

echo '---'
echo "stop_heuristic_test: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
