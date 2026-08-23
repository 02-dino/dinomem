#!/usr/bin/env bash
# test-target.sh — pick the SMALLEST meaningful deeper proof for one edited file. [v1]
#
# WHY: verify.sh/diagnose.sh catch syntax + lint, but the model still has to
# decide which project test to run next. This script mechanizes that choice for
# the common case: given one edited file, return ONE machine-parseable line with
# the narrowest repo-local test command worth running, or NONE if no meaningful
# deeper proof exists. That lets the edit-verify-loop do
#   edit -> verify -> diagnose -> test-target -> run-target
# instead of making the model rediscover the mapping every turn.
#
# CONTRACT:
#   LAST stdout line is ALWAYS exactly one of:
#     TEST_TARGET: <command>
#     TEST_TARGET: NONE
#   exit 0 when a target command was found, 1 when NONE, 2 on usage error.
#
# MAPPING (generalized, fail-open):
#   - if the file itself is already under test/ and runnable, run that file.
#   - exact test sibling wins:     test/<stem>_test.sh
#   - hook docs/handlers map by hook dir name: hooks/<hook>/{handler.ts,HOOK.md}
#                                  -> test/<hook>_test.sh if present
#   - ambiguous family matches are NOT guessed; we return NONE.
#   - missing file / outside repo / no test -> NONE (never a false target).

set -uo pipefail 2>/dev/null || true

f="${1:-}"
if [ -z "$f" ]; then
  echo "usage: test-target.sh <file>" >&2
  echo "TEST_TARGET: NONE"
  exit 2
fi

_emit_none() { echo "TEST_TARGET: NONE"; exit 1; }
_emit() { echo "TEST_TARGET: $1"; exit 0; }
_abs_path() {
  local path="$1"
  case "$path" in
    /*) printf '%s\n' "$path" ;;
    *)  printf '%s/%s\n' "$(cd "$(dirname "$path")" 2>/dev/null && pwd)" "$(basename "$path")" ;;
  esac
}
_find_repo() {
  # Walk upward from the edited file's directory. A repo root for our purposes is
  # a dir that contains BOTH test/ and scripts/. We key off the TARGET FILE, not
  # off this helper's own location, so a live analyst copy can operate on any
  # checked-out repo under the workspace.
  local cur="$1"
  [ -d "$cur" ] || cur="$(dirname "$cur")"
  while [ "$cur" != "/" ] && [ -n "$cur" ]; do
    if [ -d "$cur/test" ] && [ -d "$cur/scripts" ]; then
      printf '%s\n' "$cur"
      return 0
    fi
    cur="$(dirname "$cur")"
  done
  return 1
}
_rel_to_repo() {
  local path="$1" repo="$2"
  case "$path" in
    "$repo"/*) printf '%s\n' "${path#"$repo"/}" ;;
    *) return 1 ;;
  esac
}

abs="$(_abs_path "$f")"
[ -f "$abs" ] || _emit_none
REPO="$(_find_repo "$abs")" || _emit_none
TEST_DIR="$REPO/test"
rel="$(_rel_to_repo "$abs" "$REPO")" || _emit_none
base="$(basename "$rel")"
stem="${base%.*}"
dir="$(dirname "$rel")"

# 1) file is already a repo test -> run it directly.
case "$rel" in
  test/*.sh) _emit "bash $rel" ;;
  test/*.py) _emit "python3 $rel" ;;
  test/*.js) _emit "node $rel" ;;
  test/*.ts) _emit "tsc --noEmit $rel" ;;
 esac

# 2) exact sibling test by stem.
if [ -f "$TEST_DIR/${stem}_test.sh" ]; then
  _emit "bash test/${stem}_test.sh"
fi
if [ -f "$TEST_DIR/${stem}_test.py" ]; then
  _emit "python3 test/${stem}_test.py"
fi
if [ -f "$TEST_DIR/${stem}_test.js" ]; then
  _emit "node test/${stem}_test.js"
fi

# 3) hook fallback: hooks/<hook>/handler.ts or HOOK.md -> test/<hook>_test.sh
case "$rel" in
  hooks/*/handler.ts|hooks/*/HOOK.md)
    hook="$(basename "$(dirname "$rel")")"
    [ -f "$TEST_DIR/${hook}_test.sh" ] && _emit "bash test/${hook}_test.sh"
    ;;
 esac

# 4) single unambiguous family match by stem prefix. Multiple = guessy -> NONE.
set -- "$TEST_DIR"/"$stem"*_test.sh
if [ "$1" != "$TEST_DIR/${stem}*_test.sh" ]; then
  if [ "$#" -eq 1 ] && [ -f "$1" ]; then
    _emit "bash test/$(basename "$1")"
  fi
fi

_emit_none
