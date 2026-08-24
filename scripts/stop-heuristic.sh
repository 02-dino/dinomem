#!/usr/bin/env bash
# stop-heuristic.sh — decide whether the edit loop should continue, widen, stop,
# or distrust a shallow green. [v1]
#
# WHY: after blast-radius, repair-hint, and dependency-check, the next autonomy
# gap is LOOP JUDGMENT: knowing when another fix attempt is still high-signal,
# when the loop is bouncing and needs broader inspection, when to stop, and when
# a green is too shallow to trust. This helper keeps that judgment coarse and
# machine-parseable.
#
# CONTRACT:
#   LAST stdout line is ALWAYS exactly:
#     STOP_HEURISTIC: <CATEGORY> :: <short-guidance>
#   Categories:
#     KEEP_GOING | ESCALATE | STOP | WEAK_GREEN
#   exit 0 on any classification, 2 on usage error.
#
# INPUT:
#   stop-heuristic.sh <attempt-count> <text...>
#   stop-heuristic.sh <attempt-count>    # reads stdin if no text args supplied
#
# INTERPRETATION:
#   - attempt-count is the current fix iteration number (1..N)
#   - text is a compact summary of the most recent gate outcomes / failure text
#
# RULES (coarse, evidence-based):
#   - repeated red with high attempt count -> STOP
#   - unclear / contradictory / broad-scope red -> ESCALATE
#   - shallow green with no deeper proof -> WEAK_GREEN
#   - otherwise -> KEEP_GOING

set -uo pipefail 2>/dev/null || true

attempt="${1:-}"
shift || true
if [ -z "$attempt" ]; then
  echo "usage: stop-heuristic.sh <attempt-count> <text...>" >&2
  echo "STOP_HEURISTIC: ESCALATE :: no attempt count provided; inspect manually"
  exit 2
fi
case "$attempt" in
  ''|*[!0-9]*)
    echo "STOP_HEURISTIC: ESCALATE :: invalid attempt count; inspect manually"
    exit 2
    ;;
esac

if [ "$#" -gt 0 ]; then
  text="$*"
else
  text="$(cat 2>/dev/null || true)"
fi

emit() {
  echo "STOP_HEURISTIC: $1 :: $2"
  exit 0
}

low="$(printf '%s' "$text" | tr '[:upper:]' '[:lower:]')"
[ -n "$low" ] || emit "ESCALATE" "no outcome text supplied; inspect the raw loop state manually"

has_red=0
case "$low" in
  *"verify: fail"*|*"diagnose: issues"*|*"repair_hint:"*|*"repair_hint:"*|*"traceback"*|*"assert"*|*"failed"*|*"exception"*|*"stop_and_inspect"*|*"coordinated"*)
    has_red=1
    ;;
esac

if [ "$attempt" -ge 5 ] && [ "$has_red" -eq 1 ]; then
  emit "STOP" "five tries with red signal is enough; stop looping and report the stuck state"
fi

case "$low" in
  *"repair_hint: unknown"*|*"blast_radius: unknown"*|*"stop_and_inspect"*|*"coordinated"*|*"no outcome text supplied"*)
    emit "ESCALATE" "signal is unclear or scope is broad; widen inspection before another patch"
    ;;
esac

case "$low" in
  *"verify: pass"*|*"diagnose: clean"*)
    case "$low" in
      *"test_target: none"*)
        case "$low" in
          *"dependency_check: none"*|*"dependency_check: "*)
            emit "WEAK_GREEN" "syntax gate is green but no narrow deeper proof landed; treat this as shallow confidence"
            ;;
        esac
        ;;
    esac
    ;;
esac

if [ "$attempt" -ge 3 ] && [ "$has_red" -eq 1 ]; then
  emit "ESCALATE" "three tries with continuing red signal means inspect broader context before patching again"
fi

case "$low" in
  *"verify: fail"*|*"diagnose: issues"*|*"repair_hint: syntax"*|*"repair_hint: lint"*|*"repair_hint: config"*|*"repair_hint: fs"*|*"repair_hint: test"*|*"repair_hint: dependent_test"*)
    emit "KEEP_GOING" "signal is still actionable; make the next targeted fix and rerun the narrow gate"
    ;;
  *"verify: pass"*|*"diagnose: clean"*|*"test_target: bash "*|*"dependency_check: bash "*)
    emit "KEEP_GOING" "signal is healthy enough to continue the planned proof sequence"
    ;;
esac

emit "ESCALATE" "loop state is ambiguous; inspect manually before another automatic step"
