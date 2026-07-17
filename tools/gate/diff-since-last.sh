#!/bin/sh
# gate/diff-since-last.sh <cmd> [hit-message]
# T1 deterministic gate: runs <cmd>, hashes stdout, compares to last run.
# Prints hit-message ONLY when output changed since last run. Else empty (zero-LLM).
# Portable: sh + coreutils (sha256sum|shasum fallback) only.
set -eu
cmd="${1:?usage: diff-since-last.sh <cmd> [msg]}"
msg="${2:-output changed: $cmd}"
state_dir="${DINOMEM_GATE_STATE:-$HOME/.dinomem/gate}"
mkdir -p "$state_dir"
key=$(printf '%s' "$cmd" | tr -c 'A-Za-z0-9' '_' | cut -c1-80)
state="$state_dir/diff_$key"

out=$(sh -c "$cmd" 2>/dev/null || true)
if command -v sha256sum >/dev/null 2>&1; then
  cur=$(printf '%s' "$out" | sha256sum | awk '{print $1}')
else
  cur=$(printf '%s' "$out" | shasum -a 256 | awk '{print $1}')
fi

prev=""
[ -f "$state" ] && prev=$(cat "$state")
printf '%s' "$cur" >"$state"

[ "$cur" != "$prev" ] && [ -n "$prev" ] && printf '%s' "$msg"
exit 0
