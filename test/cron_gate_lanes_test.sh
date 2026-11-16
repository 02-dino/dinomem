#!/usr/bin/env bash
# cron_gate_lanes_test.sh — base owns the ONE cron_gate.sh (neuron ships none since
# 2026-11-16). Pins: every lane, base AND neuron, is wired (a missing lane block is
# how GATE_PENDING_REMINDER_ID went dead on neuron boxes); a lane fires only with
# BOTH its env id and its check script; stdout is exactly NO_REPLY either way.
set -uo pipefail
T="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GATE="$T/../scripts/cron_gate.sh"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/ws/scripts" "$tmp/bin"
printf '#!/bin/sh\necho "$@" >> %s/fired\n' "$tmp" > "$tmp/bin/openclaw"; chmod +x "$tmp/bin/openclaw"
rc=0
ok()  { echo "PASS $1"; }
bad() { echo "FAIL $1"; rc=1; }
# lane env key -> check script it requires
LANES="ADVANCER:check_projects.sh IMPROVER:check_improvable.sh DELETER:check_deletable.sh IDEATOR:check_ideator.sh DAILY_NOTE_REVIEW:check_daily_notes.sh PENDING_REMINDER:check_pending_notes.py AUTO_IMPROVE:check_improvement.sh STALL_RESCUER:check_stalls.sh"
run_gate() { env -i PATH="$PATH" HOME="$HOME" OPENCLAW_WORKSPACE="$tmp/ws" OPENCLAW_BIN="$tmp/bin/openclaw" "$@" bash "$GATE"; }

# 1) no env, no checks -> silent
out="$(run_gate)"; [ "$out" = NO_REPLY ] && [ ! -f "$tmp/fired" ] && ok "empty env -> NO_REPLY, nothing fired" || bad "empty env out=$out"

# 2) every lane: env id + passing check -> fires exactly that id
envs=()
for l in $LANES; do
  k="${l%%:*}"; s="${l#*:}"
  case "$s" in *.py) printf 'import sys; sys.exit(0)\n' > "$tmp/ws/scripts/$s" ;; *) printf 'exit 0\n' > "$tmp/ws/scripts/$s" ;; esac
  envs+=("GATE_${k}_ID=id-$k")
done
out="$(run_gate "${envs[@]}")"
[ "$out" = NO_REPLY ] && ok "all lanes firing -> stdout still exactly NO_REPLY" || bad "stdout=$out"
for l in $LANES; do
  k="${l%%:*}"
  [ "$(grep -c "^cron run id-$k\$" "$tmp/fired" 2>/dev/null)" = 1 ] && ok "lane $k fires once" || bad "lane $k did not fire exactly once"
done
grep -q "cron_gate: triggered" "$tmp/ws/logs/cron_gate.log" 2>/dev/null && ok "trigger info logged to logs/cron_gate.log" || bad "no trigger log"

# 3) env id set but check script absent (base-only box, neuron lanes) -> skipped
rm -f "$tmp/fired" "$tmp/ws/scripts/"*
out="$(run_gate "${envs[@]}")"
[ "$out" = NO_REPLY ] && [ ! -f "$tmp/fired" ] && ok "ids set, no check scripts -> nothing fired" || bad "missing scripts fired"
exit $rc
