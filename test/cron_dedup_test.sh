#!/usr/bin/env bash
# cron_dedup_test.sh — regression test for upsert_cron duplicate-on-upgrade bug.
#
# Bug history: the old scope "cd $WS && .*$keyword" only matched dinomem cron
# lines that START with `cd $WS &&`. Lines written via the dinomem_run.sh wrapper
# start with `DINOMEM_AGENT_ID=... bash ...` instead, so on upgrade the matcher
# found nothing and APPENDED a second copy -> silent duplicate that runs the job
# twice a day. Fix: dedup by a per-agent managed TAG, with a conservative
# one-time ADOPTION phase that migrates legacy lines without clobbering a user's
# own hand-written cron that happens to call the same script.
#
# This test extracts the ACTUAL upsert_cron from ../scripts/install.sh (not a
# copy) and drives it against a fake crontab, so it validates shipped code.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_SH="$HERE/../scripts/install.sh"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
export FAKE="$TMP/crontab.txt"
WS="/ws/agentx"; AGENT_ID="agentx"; DRY_RUN=0

# crontab shim (must survive pipe subshells, like the real binary)
crontab(){ if [ "${1:-}" = "-l" ]; then cat "$FAKE" 2>/dev/null; return 0; fi
  if [ "${1:-}" = "-" ]; then cat >"$FAKE.n" && mv "$FAKE.n" "$FAKE"; return 0; fi; return 0; }
export -f crontab
skip(){ :; }; ok(){ :; }; plan(){ :; }

awk '/^upsert_cron\(\) \{/{f=1} f{print} f&&/^}$/{exit}' "$INSTALL_SH" > "$TMP/fn.sh"
grep -q 'dinomem-managed' "$TMP/fn.sh" || { echo "FAIL: upsert_cron missing tag logic in $INSTALL_SH"; exit 1; }
source "$TMP/fn.sh"

pass=0; fail=0
ck(){ if [ "$2" = "$3" ]; then echo "  ok: $1"; pass=$((pass+1)); else echo "  FAIL: $1 (exp=$2 got=$3)"; fail=$((fail+1)); fi; }
cE(){ local n; n=$(grep -cE "$1" "$FAKE" 2>/dev/null); echo "${n:-0}"; }
cF(){ local n; n=$(grep -cF "$1" "$FAKE" 2>/dev/null); echo "${n:-0}"; }

CANON="0 5 * * * DINOMEM_AGENT_ID=$AGENT_ID bash $WS/scripts/dinomem_run.sh heavy-llm $WS python3 procedures/memory_cleanup.py >> logs/memory_cleanup.log 2>&1"
TAG="# dinomem-managed:memory_cleanup.py:$AGENT_ID"
LEGACY="20 5 * * * DINOMEM_AGENT_ID=$AGENT_ID bash $WS/scripts/dinomem_run.sh heavy-llm $WS python3 procedures/memory_cleanup.py >> $WS/logs/memory_cleanup.log 2>&1"
USER="0 18 * * * cd $WS && python3 procedures/memory_cleanup.py --deep-audit >> logs/my_audit.log 2>&1"
OTHER="0 5 * * * DINOMEM_AGENT_ID=other bash /ws/other/scripts/dinomem_run.sh heavy-llm /ws/other python3 procedures/memory_cleanup.py >> logs/memory_cleanup.log 2>&1"

: > "$FAKE"
upsert_cron memory_cleanup.py "dinomem: dedup" "$CANON" cleanup
ck "fresh install -> one tagged line" 1 "$(cE "$TAG\$")"
upsert_cron memory_cleanup.py "dinomem: dedup" "$CANON" cleanup
upsert_cron memory_cleanup.py "dinomem: dedup" "$CANON" cleanup
ck "idempotent (3x) -> still one" 1 "$(cE "$TAG\$")"

printf '%s\n' "# dinomem: dedup [agent:$AGENT_ID]" "$LEGACY" > "$FAKE"
upsert_cron memory_cleanup.py "dinomem: dedup" "$CANON" cleanup
ck "legacy upgrade -> one tagged (no duplicate)" 1 "$(cE "$TAG\$")"

printf '%s\n' "$USER" > "$FAKE"
upsert_cron memory_cleanup.py "dinomem: dedup" "$CANON" cleanup
ck "user's own same-WS cron SURVIVES" 1 "$(cF "$USER")"

printf '%s\n' "$LEGACY" "$OTHER" > "$FAKE"
upsert_cron memory_cleanup.py "dinomem: dedup" "$CANON" cleanup
ck "another agent's line untouched" 1 "$(cF "$OTHER")"

printf '%s\n' "$LEGACY" "$USER" > "$FAKE"
upsert_cron memory_cleanup.py "dinomem: dedup" "$CANON" cleanup
ck "legacy+user: legacy collapses to one tagged" 1 "$(cE "$TAG\$")"
ck "legacy+user: user still survives" 1 "$(cF "$USER")"

echo "cron_dedup_test: $pass passed, $fail failed"
[ "$fail" -eq 0 ] || exit 1
