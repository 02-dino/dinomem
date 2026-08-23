#!/usr/bin/env bash
# test_target_test.sh — pins scripts/test-target.sh's mapping contract. [v1]
#
# WHY: test-target.sh is load-bearing autonomy glue. If its mapping drifts, the
# edit-verify-loop either misses deeper proof or runs the wrong test. Pin the
# contract empirically: exact match, hook fallback, direct test pass-through,
# and clean NONE on no-match/missing-file.

set -uo pipefail 2>/dev/null || true
HERE="$(cd "$(dirname "$0")" && pwd)"
PICK="$HERE/../scripts/test-target.sh"
REPO="$(cd "$HERE/.." && pwd)"

pass=0 fail=0
_run() { out="$(bash "$PICK" "$1" 2>&1)"; rc=$?; last="$(printf '%s\n' "$out" | tail -1)"; }
_ck() { # label expected_line_fragment expected_exit
  local label="$1" want="$2" ex="$3"
  if printf '%s' "$last" | grep -Fq "$want" && [ "$rc" -eq "$ex" ]; then
    echo "  ok   $label -> $last (exit=$rc)"; pass=$((pass+1))
  else
    echo "  FAIL $label -> want [$want exit=$ex] got [$last exit=$rc]"; fail=$((fail+1))
  fi
}

_run "$REPO/scripts/verify.sh";                    _ck "exact stem match"   "TEST_TARGET: bash test/verify_test.sh" 0
_run "$REPO/scripts/diagnose.sh";                  _ck "second exact match"  "TEST_TARGET: bash test/diagnose_test.sh" 0
_run "$REPO/scripts/lib/gate_lib.sh";              _ck "lib exact match"     "TEST_TARGET: bash test/gate_lib_test.sh" 0
_run "$REPO/test/verify_test.sh";                  _ck "direct test passthru" "TEST_TARGET: bash test/verify_test.sh" 0
_run "$REPO/hooks/context-inject/handler.ts";      _ck "hook no test yet"    "TEST_TARGET: NONE" 1
_run "$REPO/hooks/context-inject/HOOK.md";         _ck "hook doc no test yet" "TEST_TARGET: NONE" 1
_run "$REPO/scripts/install.sh";                   _ck "no deeper test"      "TEST_TARGET: NONE" 1
_run "$REPO/does-not-exist.py";                    _ck "missing file"        "TEST_TARGET: NONE" 1

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
printf 'echo hi\n' > "$TMP/outside.sh"
_run "$TMP/outside.sh";                            _ck "outside repo"         "TEST_TARGET: NONE" 1

# Portability regression: a copy of the helper outside the repo must still work
# by discovering the repo from the TARGET FILE path, not from its own location.
cp "$PICK" "$TMP/test-target-copy.sh"
out="$(bash "$TMP/test-target-copy.sh" "$REPO/scripts/verify.sh" 2>&1)"; rc=$?; last="$(printf '%s\n' "$out" | tail -1)"
_ck "copied helper still resolves repo" "TEST_TARGET: bash test/verify_test.sh" 0

echo "---"
echo "test_target_test: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
