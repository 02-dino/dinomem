#!/usr/bin/env bash
# repair_hint_test.sh — pins scripts/repair-hint.sh classification contract. [v1]
#
# WHY: repair-hint.sh is the routing brain for the next fix attempt. If the
# categories drift, the edit loop gets dumber again. Keep the categories coarse,
# but prove the common failure families: syntax, lint, config, direct test,
# dependent test, fs, unknown.

set -uo pipefail 2>/dev/null || true
HERE="$(cd "$(dirname "$0")" && pwd)"
HINT="$HERE/../scripts/repair-hint.sh"

pass=0 fail=0
_run() { out="$(bash "$HINT" "$1" "$2" 2>&1)"; rc=$?; last="$(printf '%s\n' "$out" | tail -1)"; }
_ck() {
  local label="$1" want="$2" ex="$3"
  if printf '%s' "$last" | grep -Fq "$want" && [ "$rc" -eq "$ex" ]; then
    echo "  ok   $label -> $last (exit=$rc)"; pass=$((pass+1))
  else
    echo "  FAIL $label -> want [$want exit=$ex] got [$last exit=$rc]"; fail=$((fail+1))
  fi
}

_run verify 'VERIFY: FAIL bad.py (py_compile) :: SyntaxError: invalid syntax'
_ck 'verify syntax' 'REPAIR_HINT: SYNTAX ::' 0

_run verify 'VERIFY: FAIL nope.py (fs) :: file does not exist'
_ck 'verify fs' 'REPAIR_HINT: FS ::' 0

_run diagnose 'DIAGNOSE: ISSUES bad.py (ruff) :: 1 finding(s); first: F821 undefined name `x`'
_ck 'diagnose lint' 'REPAIR_HINT: LINT ::' 0

_run diagnose 'DIAGNOSE: ISSUES bad.json (jq) :: 1 finding(s); first: jq: parse error: Unfinished JSON term at EOF'
_ck 'diagnose config' 'REPAIR_HINT: CONFIG ::' 0

_run test 'verify_test: 8 passed, 1 failed'
_ck 'direct test failure' 'REPAIR_HINT: TEST ::' 0

_run dependency-test 'gate_lib_test: 19 passed, 1 failed'
_ck 'dependent test failure' 'REPAIR_HINT: DEPENDENT_TEST ::' 0

_run diagnose 'something weird that matches nothing'
_ck 'unknown' 'REPAIR_HINT: LINT ::' 0

out="$(printf 'custom traceback noise' | bash "$HINT" test 2>&1)"; rc=$?; last="$(printf '%s\n' "$out" | tail -1)"
_ck 'stdin input' 'REPAIR_HINT: TEST ::' 0

echo '---'
echo "repair_hint_test: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
