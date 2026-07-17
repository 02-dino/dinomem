#!/bin/sh
# gate/threshold.sh <cmd> <op> <value> [hit-message]
# T1 deterministic gate: runs <cmd>, reads first numeric token of stdout,
# compares numeric <op> in {lt,le,gt,ge,eq,ne} against <value>.
# Prints hit-message ONLY when the comparison is TRUE. Else empty (zero-LLM).
# Portable: sh + awk only.
set -eu
cmd="${1:?usage: threshold.sh <cmd> <op> <value> [msg]}"
op="${2:?op: lt|le|gt|ge|eq|ne}"
val="${3:?value}"
msg="${4:-threshold hit: $cmd $op $val}"

raw=$(sh -c "$cmd" 2>/dev/null || true)
num=$(printf '%s' "$raw" | grep -oE -- '-?[0-9]+([.][0-9]+)?' | head -n1)
[ -n "${num:-}" ] || exit 0   # no numeric output -> no hit

hit=$(awk -v a="$num" -v b="$val" -v op="$op" 'BEGIN{
  if(op=="lt")print(a<b);else if(op=="le")print(a<=b);
  else if(op=="gt")print(a>b);else if(op=="ge")print(a>=b);
  else if(op=="eq")print(a==b);else if(op=="ne")print(a!=b);
  else print 0}')
[ "$hit" = "1" ] && printf '%s (value=%s)' "$msg" "$num"
exit 0
