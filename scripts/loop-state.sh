#!/usr/bin/env bash
# loop-state.sh — tiny per-file scratch state for one edit loop. [v1]
#
# WHY: the edit loop can forget the exact last failed move and repeat it. This
# helper stores only the smallest useful scratch state per file so the next retry
# can detect "same gate + same repair class + same error + same move" and avoid
# blind repetition. Per-turn temp state only; no daemon, no DB, no cross-turn plan.
#
# CONTRACT:
#   LAST stdout line is ALWAYS exactly one of:
#     LOOP_STATE: RECORDED :: <state-file>
#     LOOP_STATE: FRESH :: no matching prior move
#     LOOP_STATE: REPEAT :: same move already tried for this loop
#     LOOP_STATE: CLEARED :: <state-file>
#     LOOP_STATE: EMPTY :: no state for file
#   exit 0 on RECORDED/FRESH/CLEARED/EMPTY, 1 on REPEAT, 2 on usage error.
#
# USAGE:
#   loop-state.sh record <file> <attempt> <gate> <repair> <error> <move>
#   loop-state.sh check  <file> <gate> <repair> <error> <move>
#   loop-state.sh clear  <file>
#   loop-state.sh show   <file>

set -uo pipefail 2>/dev/null || true

ROOT="${DINOMEM_LOOP_STATE_DIR:-/tmp/dinomem_loop_state}"
mkdir -p "$ROOT" 2>/dev/null || true

emit() {
  echo "LOOP_STATE: $1 :: $2"
  exit "${3:-0}"
}

usage() {
  echo "usage: loop-state.sh <record|check|clear|show> ..." >&2
  emit "EMPTY" "invalid usage" 2
}

state_path() {
  python3 - "$1" "$ROOT" <<'PY'
import hashlib, os, sys
f = os.path.abspath(sys.argv[1])
root = sys.argv[2]
key = hashlib.sha1(f.encode()).hexdigest()
print(os.path.join(root, key + '.state'))
PY
}

read_field() {
  python3 - "$1" "$2" <<'PY'
import json, sys
path, key = sys.argv[1], sys.argv[2]
try:
    with open(path, 'r', encoding='utf-8') as fh:
        data = json.load(fh)
except Exception:
    print("")
    raise SystemExit(0)
print(data.get(key, ""))
PY
}

cmd="${1:-}"
[ -n "$cmd" ] || usage
shift || true

case "$cmd" in
  record)
    [ "$#" -eq 6 ] || usage
    file="$1"; attempt="$2"; gate="$3"; repair="$4"; err="$5"; move="$6"
    sp="$(state_path "$file")"
    python3 - "$sp" "$file" "$attempt" "$gate" "$repair" "$err" "$move" <<'PY'
import json, os, sys
sp, file, attempt, gate, repair, err, move = sys.argv[1:8]
os.makedirs(os.path.dirname(sp), exist_ok=True)
data = {
    'file': os.path.abspath(file),
    'attempt': attempt,
    'gate': gate,
    'repair': repair,
    'error': err,
    'move': move,
}
with open(sp, 'w', encoding='utf-8') as fh:
    json.dump(data, fh, ensure_ascii=False)
PY
    emit "RECORDED" "$sp" 0
    ;;
  check)
    [ "$#" -eq 5 ] || usage
    file="$1"; gate="$2"; repair="$3"; err="$4"; move="$5"
    sp="$(state_path "$file")"
    [ -f "$sp" ] || emit "FRESH" "no matching prior move" 0
    old_gate="$(read_field "$sp" gate)"
    old_repair="$(read_field "$sp" repair)"
    old_err="$(read_field "$sp" error)"
    old_move="$(read_field "$sp" move)"
    if [ "$old_gate" = "$gate" ] && [ "$old_repair" = "$repair" ] && [ "$old_err" = "$err" ] && [ "$old_move" = "$move" ]; then
      emit "REPEAT" "same move already tried for this loop" 1
    fi
    emit "FRESH" "no matching prior move" 0
    ;;
  clear)
    [ "$#" -eq 1 ] || usage
    file="$1"
    sp="$(state_path "$file")"
    rm -f "$sp"
    emit "CLEARED" "$sp" 0
    ;;
  show)
    [ "$#" -eq 1 ] || usage
    file="$1"
    sp="$(state_path "$file")"
    [ -f "$sp" ] || emit "EMPTY" "no state for file" 0
    python3 - "$sp" <<'PY'
import json, sys
with open(sys.argv[1], 'r', encoding='utf-8') as fh:
    data = json.load(fh)
print(json.dumps(data, ensure_ascii=False, sort_keys=True))
PY
    ;;
  *)
    usage
    ;;
esac
