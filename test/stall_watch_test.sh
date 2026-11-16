#!/usr/bin/env bash
# stall_watch_test.sh — runs the stall_watch.py selftest (synthetic sessions.json
# fixtures: failed, timeout, ghost, done, rules a-e, ledger once, restart budget)
# plus the check_stalls.sh wrapper's fail-open edge. Exit non-zero on any FAIL.
set -uo pipefail
T="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
S="$T/../scripts"
rc=0
python3 "$S/stall_watch.py" selftest || rc=1
# wrapper edge: no sessions store -> exit 1 (lane quiet), never a crash/traceback
out="$(OPENCLAW_WORKSPACE=/nonexistent DINOMEM_AGENT_SESSIONS=/nonexistent STALL_LEDGER=/tmp/stall_test_$$.json bash "$S/check_stalls.sh" 2>&1)"; wrc=$?
if [ "$wrc" = 1 ] && ! printf '%s' "$out" | grep -q Traceback; then
  echo "PASS check_stalls.sh missing store -> exit 1"
else
  echo "FAIL check_stalls.sh missing store -> rc=$wrc out=$out"; rc=1
fi
rm -f "/tmp/stall_test_$$.json"
exit $rc
