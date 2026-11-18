#!/usr/bin/env bash
# cross_instance_exec_warn_test.sh — pins plugins/cross-instance-exec-warn detection
# + scripts/run-as-instance.sh env contract.
# WHY: the plugin is the only thing that catches a foreign-instance exec at the
# moment it happens; a regex drift either goes silent (bug class returns) or
# cries wolf on every cat/grep (warning gets ignored). Both are pinned here.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
rc=0
node "$REPO/plugins/cross-instance-exec-warn/detect.test.mjs" || rc=1

T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
mkdir -p "$T/.openclaw" "$T/.openclaw-cs"
echo '{}' > "$T/.openclaw/openclaw.json"; echo '{}' > "$T/.openclaw-cs/openclaw.json"
out="$(OPENCLAW_STATE_DIR="$T/.openclaw-x" OPENCLAW_GATEWAY_PORT=1 bash "$REPO/scripts/run-as-instance.sh" cs env)"
grep -qx "OPENCLAW_CONFIG_PATH=$T/.openclaw-cs/openclaw.json" <<<"$out" || { echo "FAIL run-as-instance CONFIG_PATH"; rc=1; }
grep -q "^OPENCLAW_GATEWAY_PORT=" <<<"$out" && { echo "FAIL run-as-instance kept inherited port"; rc=1; }
OPENCLAW_STATE_DIR="$T/.openclaw-x" bash "$REPO/scripts/run-as-instance.sh" nope true 2>/dev/null && { echo "FAIL unknown instance should exit 2"; rc=1; }
[ $rc = 0 ] && echo "PASS cross_instance_exec_warn_test" || echo "FAIL cross_instance_exec_warn_test"
exit $rc
