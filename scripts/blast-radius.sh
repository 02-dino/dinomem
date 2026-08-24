#!/usr/bin/env bash
# blast-radius.sh — classify the likely edit scope before patching one file. [v1]
#
# WHY: after adding verify/test-target/dependency-check/repair-hint, the next
# avoidable failure is a BAD FIRST PATCH: treating a shared/helper change like a
# trivial one-file edit. This helper reuses the existing proof pickers to answer
# one narrow question before editing: does the current evidence look like a
# single-file proof, a coordinated change, a stop-and-inspect case, or unknown?
#
# CONTRACT:
#   LAST stdout line is ALWAYS exactly:
#     BLAST_RADIUS: <CATEGORY> :: <short-guidance>
#   Categories:
#     SINGLE_FILE | COORDINATED | STOP_AND_INSPECT | UNKNOWN
#   exit 0 on any classification, 2 on usage error.
#
# INPUT:
#   blast-radius.sh <file>
#
# RULES (conservative, concrete-evidence only):
#   - if the file is missing / outside a repo root we understand -> UNKNOWN
#   - if dependency-check finds dependent proofs -> COORDINATED
#   - else if test-target finds a direct proof -> SINGLE_FILE
#   - else if the path smells shared/load-bearing (scripts/lib/, hooks/, install/update)
#     and there is no narrow direct proof -> STOP_AND_INSPECT
#   - else -> UNKNOWN

set -uo pipefail 2>/dev/null || true

f="${1:-}"
if [ -z "$f" ]; then
  echo "usage: blast-radius.sh <file>" >&2
  echo "BLAST_RADIUS: UNKNOWN :: no file provided; inspect scope manually"
  exit 2
fi

_emit() {
  echo "BLAST_RADIUS: $1 :: $2"
  exit 0
}
_abs_path() {
  local path="$1"
  case "$path" in
    /*) printf '%s\n' "$path" ;;
    *)  printf '%s/%s\n' "$(cd "$(dirname "$path")" 2>/dev/null && pwd)" "$(basename "$path")" ;;
  esac
}
_find_repo() {
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

abs="$(_abs_path "$f")"
[ -f "$abs" ] || _emit "UNKNOWN" "file missing or not yet created; inspect intended scope manually"
REPO="$(_find_repo "$abs")" || _emit "UNKNOWN" "repo root not recognized from target path; inspect scope manually"
TEST_TARGET="$REPO/scripts/test-target.sh"
DEP_CHECK="$REPO/scripts/dependency-check.sh"
rel="${abs#"$REPO/"}"

if [ -x "$DEP_CHECK" ]; then
  dep_out="$(bash "$DEP_CHECK" "$abs" 2>/dev/null || true)"
  dep_last="$(printf '%s\n' "$dep_out" | tail -1)"
  case "$dep_last" in
    'DEPENDENCY_CHECK: '*)
      dep_cmds="${dep_last#DEPENDENCY_CHECK: }"
      if [ -n "$dep_cmds" ] && [ "$dep_cmds" != "NONE" ]; then
        _emit "COORDINATED" "dependent proofs exist ($dep_cmds); plan a coordinated change, not a one-file patch"
      fi
      ;;
  esac
fi

shared_risk=0
case "$rel" in
  scripts/lib/*|hooks/*|scripts/install.sh|scripts/update.sh|scripts/uninstall.sh)
    shared_risk=1
    ;;
esac

if [ -x "$TEST_TARGET" ]; then
  tt_out="$(bash "$TEST_TARGET" "$abs" 2>/dev/null || true)"
  tt_last="$(printf '%s\n' "$tt_out" | tail -1)"
  case "$tt_last" in
    'TEST_TARGET: '*)
      tt_cmd="${tt_last#TEST_TARGET: }"
      if [ -n "$tt_cmd" ] && [ "$tt_cmd" != "NONE" ]; then
        if [ "$shared_risk" -eq 1 ]; then
          _emit "STOP_AND_INSPECT" "shared or load-bearing path has only direct proof ($tt_cmd); inspect callers/tests before patching"
        fi
        _emit "SINGLE_FILE" "narrow direct proof exists ($tt_cmd); start with a one-file patch unless new evidence appears"
      fi
      ;;
  esac
fi

if [ "$shared_risk" -eq 1 ]; then
  _emit "STOP_AND_INSPECT" "shared or load-bearing path with no narrow proof; inspect callers/tests before patching"
fi

_emit "UNKNOWN" "no direct proof or dependent evidence found; inspect scope manually before patching"
