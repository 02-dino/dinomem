#!/usr/bin/env bash
# cron_fn_parity_test.sh — ANTI-DRIFT guard for upsert_cron.
#
# upsert_cron is hand-maintained in TWO installers: dinomem base
# (scripts/install.sh) and dinomem-neuron (scripts/install.sh). They must stay
# logically identical or a one-sided edit silently reintroduces the
# duplicate-on-upgrade bug in only one layer. This test extracts both function
# bodies and asserts they are identical AFTER removing the ONE sanctioned
# difference: base carries `DRY_RUN`/plan guard lines that neuron omits.
#
# If this fails: you edited one copy and not the other. Port the change.
#
# Locating neuron: env DINOMEM_NEURON_INSTALL overrides; else try common paths.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_SH="$HERE/../scripts/install.sh"

find_neuron() {
  [ -n "${DINOMEM_NEURON_INSTALL:-}" ] && { echo "$DINOMEM_NEURON_INSTALL"; return; }
  local c
  for c in \
    "$HERE/../../dinomem-neuron/scripts/install.sh" \
    "$HERE/../../../dinomem-neuron/scripts/install.sh" \
    "$HERE/../../github/dinomem-neuron/scripts/install.sh" \
    /root/.openclaw/workspace-analyst/github/dinomem-neuron/scripts/install.sh ; do
    [ -f "$c" ] && { echo "$c"; return; }
  done
  echo ""
}
NEURON_SH="$(find_neuron)"

extract(){ awk '/^upsert_cron\(\) \{/{f=1} f{print} f&&/^}$/{exit}' "$1"; }
# Strip the sanctioned base-only lines (DRY_RUN guards + the plan-branch comment)
# so we compare the CORE dedup/adoption logic only.
canon(){ extract "$1" | grep -vE 'DRY_RUN|Content differs — replace \(only'; }

fail=0
[ -f "$BASE_SH" ] || { echo "SKIP: base install.sh not found ($BASE_SH)"; exit 0; }
if [ -z "$NEURON_SH" ] || [ ! -f "$NEURON_SH" ]; then
  echo "SKIP: neuron install.sh not found (set DINOMEM_NEURON_INSTALL to enable parity check)"; exit 0
fi

# Both must actually contain the fix (guards against 'both reverted' passing trivially)
for f in "$BASE_SH" "$NEURON_SH"; do
  n=$(extract "$f" | grep -cE 'dinomem-managed|dino_sig'); [ "${n:-0}" -ge 2 ] || { echo "FAIL: $f upsert_cron missing tag/adoption logic"; fail=1; }
done

if diff <(canon "$BASE_SH") <(canon "$NEURON_SH") >/dev/null; then
  echo "ok: upsert_cron core logic IDENTICAL across base + neuron (no drift)"
else
  echo "FAIL: upsert_cron DRIFTED between base and neuron:"
  diff <(canon "$BASE_SH") <(canon "$NEURON_SH") | sed 's/^/    /'
  fail=1
fi

echo "cron_fn_parity_test: $([ "$fail" = 0 ] && echo PASS || echo FAIL)"
exit "$fail"
