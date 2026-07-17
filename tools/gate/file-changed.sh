#!/bin/sh
# gate/file-changed.sh <path> [hit-message]
# T1 deterministic gate: prints hit-message ONLY when <path> changed since last run.
# Empty stdout = no change = zero-LLM no-op. State kept in a sibling dotfile.
# Portable: sh + coreutils (sha256sum|shasum fallback) only.
set -eu
path="${1:?usage: file-changed.sh <path> [msg]}"
msg="${2:-changed: $path}"
state_dir="${DINOMEM_GATE_STATE:-$HOME/.dinomem/gate}"
mkdir -p "$state_dir"
key=$(printf '%s' "$path" | tr -c 'A-Za-z0-9' '_')
state="$state_dir/file-changed_$key"

[ -e "$path" ] || { : >"$state"; exit 0; }   # missing -> no hit, reset

if command -v sha256sum >/dev/null 2>&1; then
  cur=$(sha256sum "$path" | awk '{print $1}')
else
  cur=$(shasum -a 256 "$path" | awk '{print $1}')
fi

prev=""
[ -f "$state" ] && prev=$(cat "$state")
printf '%s' "$cur" >"$state"

[ "$cur" != "$prev" ] && [ -n "$prev" ] && printf '%s' "$msg"
exit 0
