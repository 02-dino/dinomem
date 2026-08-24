#!/usr/bin/env bash
# dependency-check.sh — pick the smallest affected dependent tests for one edit. [v1]
#
# WHY: test-target.sh proves the edited file itself, but shared helpers can still
# break nearby callers/importers. This script adds the next layer: when a code
# graph is available, ask it for immediate dependents of the edited file's
# contained symbols, then reuse test-target.sh to pick the narrowest tests for
# those dependents. No graph/tool -> clean NONE (base-safe, never guessy).
#
# CONTRACT:
#   LAST stdout line is ALWAYS exactly one of:
#     DEPENDENCY_CHECK: <command> || <command> ...
#     DEPENDENCY_CHECK: NONE
#   exit 0 when dependent test command(s) were found, 1 when NONE, 2 on usage.

set -uo pipefail 2>/dev/null || true

f="${1:-}"
if [ -z "$f" ]; then
  echo "usage: dependency-check.sh <file>" >&2
  echo "DEPENDENCY_CHECK: NONE"
  exit 2
fi

_emit_none() { echo "DEPENDENCY_CHECK: NONE"; exit 1; }
_emit() { echo "DEPENDENCY_CHECK: $1"; exit 0; }
_abs_path() {
  local path="$1"
  case "$path" in
    /*) printf '%s\n' "$path" ;;
    *)  printf '%s/%s\n' "$(cd "$(dirname "$path")" 2>/dev/null && pwd)" "$(basename "$path")" ;;
  esac
}
_find_repo() {
  local cur="$1"
  [ -d "$cur" ] || cur="$(dirname "$cur")"
  while [ "$cur" != "/" ] && [ -n "$cur" ]; do
    if [ -d "$cur/test" ] && [ -d "$cur/scripts" ]; then
      printf '%s\n' "$cur"
      return 0
    fi
    cur="$(dirname "$cur")"
  done
  return 1
}
_find_code_query() {
  local repo="$1" cand
  for cand in \
    "${DINOMEM_CODE_QUERY:-}" \
    "${OPENCLAW_WORKSPACE:-}/tools/code_query.py" \
    "${DINOMEM_WORKSPACE:-}/tools/code_query.py" \
    "$repo/tools/code_query.py"
  do
    [ -n "$cand" ] || continue
    [ -f "$cand" ] && { printf '%s\n' "$cand"; return 0; }
  done
  return 1
}
_json_get() { python3 -c "$1"; }

abs="$(_abs_path "$f")"
[ -f "$abs" ] || _emit_none
REPO="$(_find_repo "$abs")" || _emit_none
TEST_TARGET="$REPO/scripts/test-target.sh"
[ -x "$TEST_TARGET" ] || _emit_none
CQ="$(_find_code_query "$REPO")" || _emit_none

base="$(basename "$abs")"
stem="${base%.*}"
repo_rel="${abs#"$REPO"/}"

file_json="$(python3 "$CQ" explain "$stem" --json 2>/dev/null || true)"
[ -n "$file_json" ] || _emit_none

parsed="$(JSON_INPUT="$file_json" python3 - "$repo_rel" <<'PY'
import json, os, re, sys
j = json.loads(os.environ.get("JSON_INPUT") or "{}")
node = j.get("node", "")
m = re.search(r"\(([^()]+):(\d+)\)\s*$", node)
node_path = m.group(1) if m else ""
prefix = ""
repo_rel = sys.argv[1]
if node_path.endswith(repo_rel):
    prefix = node_path[:-len(repo_rel)]
syms = []
for item in (j.get("outgoing", {}).get("contains") or []):
    tgt = item.get("target", "")
    mpath = re.search(r"\(([^()]+):(\d+)\)\s*$", tgt)
    if not mpath or mpath.group(1) != node_path:
        continue
    msym = re.match(r"(?:function|method|class)\s+([^\s«]+)", tgt)
    if msym:
        syms.append(msym.group(1))
print(prefix)
print("---")
for s in syms[:12]:
    print(s)
PY
)"

graph_prefix="$(printf '%s\n' "$parsed" | sed -n '1p')"
symbols="$(printf '%s\n' "$parsed" | sed '1,2d')"
[ -n "$symbols" ] || _emit_none

symbol_jsons="$(while IFS= read -r sym; do
  [ -n "$sym" ] || continue
  python3 "$CQ" explain "$sym" --json 2>/dev/null || true
  printf '\n'
done <<< "$symbols")"

dep_files="$(JSON_INPUT="$symbol_jsons" python3 - "$repo_rel" "$graph_prefix" <<'PY'
import json, os, re, sys
seen = []
this_file = sys.argv[1]
prefix = sys.argv[2]
for chunk in (os.environ.get("JSON_INPUT") or "").splitlines():
    line = chunk.strip()
    if not line or not line.startswith("{"):
        continue
    try:
        j = json.loads(line)
    except Exception:
        continue
    for cb in (j.get("called_by") or []):
        src = cb.get("source", "")
        m = re.search(r"\(([^()]+):(\d+)\)\s*$", src)
        if not m:
            continue
        path = m.group(1)
        if prefix and path.startswith(prefix):
            path = path[len(prefix):]
        if path == this_file:
            continue
        if path not in seen:
            seen.append(path)
for p in seen:
    print(p)
PY
)"

[ -n "$dep_files" ] || _emit_none

cmds="$(while IFS= read -r dep; do
  [ -n "$dep" ] || continue
  dep_abs="$REPO/$dep"
  [ -f "$dep_abs" ] || continue
  out="$(bash "$TEST_TARGET" "$dep_abs" 2>/dev/null || true)"
  last="$(printf '%s\n' "$out" | tail -1)"
  case "$last" in
    'TEST_TARGET: '*) cmd="${last#TEST_TARGET: }"; [ "$cmd" != "NONE" ] && printf '%s\n' "$cmd" ;;
  esac
done <<< "$dep_files" | python3 -c 'import sys
seen=[]
for line in sys.stdin:
    s=line.strip()
    if s and s not in seen: seen.append(s)
print(" || ".join(seen))
')"

[ -n "$cmds" ] || _emit_none
_emit "$cmds"
