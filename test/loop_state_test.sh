#!/usr/bin/env bash
# loop_state_test.sh — pins scripts/loop-state.sh anti-repeat contract. [v1]
#
# WHY: loop-state should stay tiny and useful. It must remember only the last
# move for one file, detect exact repeats, and clear cleanly.

set -uo pipefail 2>/dev/null || true
HERE="$(cd "$(dirname "$0")" && pwd)"
STATE="$HERE/../scripts/loop-state.sh"
TMPDIRX="$(mktemp -d)"
trap 'rm -rf "$TMPDIRX"' EXIT
FILE="$TMPDIRX/demo.py"
printf 'x=1\n' > "$FILE"

pass=*** fail=0
_run() { out="$(DINOMEM_LOOP_STATE_DIR="$TMPDIRX/state" bash "$STATE" "$@" 2>&1)"; rc=$?; last="$(printf '%s\n' "$out" | tail -1)"; }
_ck() {
  local label="$1" want="$2" ex="$3"
  if printf '%s' "$last" | grep -Fq "$want" && [ "$rc" -eq "$ex" ]; then
    echo "  ok   $label -> $last (exit=$rc)"; pass=***
  else
    echo "  FAIL $label -> want [$want exit=$ex] got [$last exit=$rc]"; fail=$((fail+1))
  fi
}

_run check "$FILE" verify SYNTAX 'SyntaxError line 1' 'rename var'
_ck 'fresh before record' 'LOOP_STATE: FRESH ::' 0

_run record "$FILE" 1 verify SYNTAX 'SyntaxError line 1' 'rename var'
_ck 'record state' 'LOOP_STATE: RECORDED ::' 0

_run check "$FILE" verify SYNTAX 'SyntaxError line 1' 'rename var'
_ck 'repeat detected' 'LOOP_STATE: REPEAT ::' 1

_run check "$FILE" verify SYNTAX 'SyntaxError line 1' 'fix indent'
_ck 'different move is fresh' 'LOOP_STATE: FRESH ::' 0

show_out="$(DINOMEM_LOOP_STATE_DIR="$TMPDIRX/state" bash "$STATE" show "$FILE" 2>&1)"; rc=$?; last="$(printf '%s\n' "$show_out" | tail -1)"
if printf '%s' "$last" | grep -Fq '"attempt": "1"' && printf '%s' "$last" | grep -Fq '"move": "rename var"' && [ "$rc" -eq 0 ]; then
  echo "  ok   show state -> $last (exit=$rc)"; pass=***
else
  echo "  FAIL show state -> unexpected [$last exit=$rc]"; fail=$((fail+1))
fi

_run clear "$FILE"
_ck 'clear state' 'LOOP_STATE: CLEARED ::' 0

_run show "$FILE"
_ck 'empty after clear' 'LOOP_STATE: EMPTY ::' 0

echo '---'
echo "loop_state_test: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
