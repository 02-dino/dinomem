#!/usr/bin/env bash
# run-as-instance.sh <instance|default> <cmd...>
# WHY: every exec from a gateway inherits the CALLER's OPENCLAW_CONFIG_PATH /
# OPENCLAW_STATE_DIR / OPENCLAW_GATEWAY_PORT. A script belonging to another
# instance on the same box then resolves the caller's config and talks to the
# caller's gateway. Just UNSETTING them is also wrong: with no CONFIG_PATH the
# resolver falls back to <home>/.openclaw/openclaw.json, which is yet another
# instance. So point CONFIG_PATH + STATE_DIR at the target explicitly and drop
# the port so the target's own config decides it.
# Paired with plugins/cross-instance-exec-warn, whose warning names this script.
set -euo pipefail
if [ $# -lt 2 ]; then
  echo "usage: $0 <instance|default> <cmd...>   (instance = suffix of <home>/.openclaw-<instance>)" >&2
  exit 2
fi
inst="$1"; shift
# Instances live side by side under one home; derive it from the caller's own
# state dir (works when HOME differs from the gateway's home), else $HOME.
if [ -n "${OPENCLAW_STATE_DIR:-}" ]; then home="$(dirname "${OPENCLAW_STATE_DIR%/}")"; else home="${HOME:-/root}"; fi
if [ "$inst" = "default" ]; then dir="$home/.openclaw"; else dir="$home/.openclaw-$inst"; fi
if [ ! -f "$dir/openclaw.json" ]; then
  echo "run-as-instance: no config at $dir/openclaw.json. instances here:" >&2
  for d in "$home"/.openclaw "$home"/.openclaw-*; do
    [ -f "$d/openclaw.json" ] || continue
    b="$(basename "$d")"; [ "$b" = ".openclaw" ] && echo "  default" >&2 || echo "  ${b#.openclaw-}" >&2
  done
  exit 2
fi
exec env -u OPENCLAW_GATEWAY_PORT \
  OPENCLAW_CONFIG_PATH="$dir/openclaw.json" \
  OPENCLAW_STATE_DIR="$dir" \
  "$@"
