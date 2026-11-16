#!/usr/bin/env bash
# cron_stagger_unique_test.sh — regression test for */15 auto_session_reset
# offset collisions on multi-agent boxes (md5(agent)%15 bunched 12 agents onto
# shared minutes). Extracts the ACTUAL pick_reset_offset from install.sh (not a
# copy) and drives it against a fake crontab.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_SH="$HERE/../scripts/install.sh"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
export FAKE="$TMP/crontab.txt"
crontab(){ if [ "${1:-}" = "-l" ]; then cat "$FAKE" 2>/dev/null; return 0; fi; return 0; }
export -f crontab

awk '/^pick_reset_offset\(\) \{/{f=1} f{print} f&&/^}$/{exit}' "$INSTALL_SH" > "$TMP/fn.sh"
grep -q 'auto_session_reset' "$TMP/fn.sh" || { echo "FAIL: pick_reset_offset missing in $INSTALL_SH"; exit 1; }
source "$TMP/fn.sh"

pass=0; fail=0
ck(){ if [ "$2" = "$3" ]; then echo "  ok: $1"; pass=$((pass+1)); else echo "  FAIL: $1 (exp=$2 got=$3)"; fail=$((fail+1)); fi; }
L(){ printf '%s * * * * cd /ws/%s && timeout 840 python3 procedures/auto_session_reset.py >> logs/auto_reset.log 2>&1 # dinomem-managed:auto_session_reset.py:%s\n' "$1" "$2" "$2"; }

: > "$FAKE"
ck "single agent, empty crontab -> hash offset unchanged" 7 "$(pick_reset_offset solo 7)"
L 7,22,37,52 solo > "$FAKE"
ck "single agent, own line -> unchanged" 7 "$(pick_reset_offset solo 7)"

{ L 2,17,32,47 me; L 5,20,35,50 other; } > "$FAKE"
ck "existing own line kept even if hash differs" 2 "$(pick_reset_offset me 5)"

{ L 5,20,35,50 a; L 6,21,36,51 b; } > "$FAKE"
ck "collision probes +1,+2 -> first free" 7 "$(pick_reset_offset new 5)"
{ L 14,29,44,59 a; L 0,15,30,45 b; } > "$FAKE"
ck "probe wraps mod 15" 1 "$(pick_reset_offset new 14)"
{ L 5,20,35,50 a; echo "# 6,21,36,51 * * * * old # dinomem-managed:auto_session_reset.py:b"; } > "$FAKE"
ck "commented-out lines ignored" 6 "$(pick_reset_offset new 5)"

: > "$FAKE"; for i in $(seq 0 14); do L "$i,$((i+15)),$((i+30)),$((i+45))" "ag$i" >> "$FAKE"; done
ck "all 15 taken -> hash fallback" 9 "$(pick_reset_offset new 9)"

echo "cron_stagger_unique_test: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
