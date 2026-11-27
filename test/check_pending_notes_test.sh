#!/usr/bin/env bash
# check_pending_notes_test.sh — pins the reminder-lane contract: a due note fires
# ONCE, then stays quiet for the cooldown (the bug: it re-fired every 15-min gate
# tick, 2026-11-27); remind_on overrides the computed date; cooldown 0 = old
# behaviour; a corrupt state file fails open.
set -uo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$HERE/scripts/check_pending_notes.py"
pass=0; fail=0
ok()  { echo "ok   - $*"; pass=$((pass+1)); }
bad() { echo "FAIL - $*"; fail=$((fail+1)); }
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
export DINOMEM_MEMORY_DIR="$tmp"
run() { python3 -W error "$SCRIPT" >/dev/null 2>&1; echo $?; }
note() { # $1 name, $2 date, $3 extra frontmatter line (optional)
  printf -- '---\ntype: task_bound\nstatus: pending\ndate: %s\n%s---\n# %s\n' "$2" "${3:-}" "$1" > "$tmp/_note_$1.md"
}
old="$(date -u -d '10 days ago' +%F)"
future="$(date -u -d '5 days' +%F)"

[ "$(run)" = 1 ] && ok "empty memory dir -> exit 1" || bad "empty dir did not exit 1"

note due "$old"
[ "$(run)" = 0 ] && ok "due note -> fires" || bad "due note did not fire"
[ "$(run)" = 1 ] && ok "same note next tick -> silent (cooldown)" || bad "re-fired inside cooldown (the spam bug)"
[ "$(PENDING_REMINDER_COOLDOWN_H=0 run)" = 0 ] && ok "cooldown 0 -> fires every tick" || bad "cooldown 0 not honoured"

rm -f "$tmp"/_note_* "$tmp/.pending_reminder_state.json"
note later "$old" "remind_on: $future
"
[ "$(run)" = 1 ] && ok "remind_on in future -> silent despite old date" || bad "remind_on ignored"

note done_one "$old"; sed -i 's/status: pending/status: done/' "$tmp/_note_done_one.md"
[ "$(run)" = 1 ] && ok "status done -> silent" || bad "done note fired"

rm -f "$tmp"/_note_*; note due2 "$old"
echo '{not json' > "$tmp/.pending_reminder_state.json"
[ "$(run)" = 0 ] && ok "corrupt state -> fail-open, fires" || bad "corrupt state blocked reminder"
python3 -c "import json,sys; d=json.load(open('$tmp/.pending_reminder_state.json')); sys.exit(0 if list(d)==['_note_due2.md'] else 1)" \
  && ok "state rewritten, closed notes pruned" || bad "state not rewritten/pruned"

echo "# $pass passed, $fail failed"
[ "$fail" = 0 ]
