#!/usr/bin/env bash
# verify.sh — zero-config in-turn code checker for the edit-verify-loop.  [v1]
#
# WHY (the frame): closes IDE gap #1 (real-time edit->run->fix loop) WITHOUT a
# cron or a coding-agent subprocess. After the agent edits a code file, it runs
#   bash scripts/verify.sh <file>
# and reads a single machine-parseable verdict line. On FAIL it reads the first
# error, fixes, re-runs — all IN THE SAME TURN. That turns request->response
# into request->(edit->verify->fix)xN->response, which is the whole IDE edge.
#
# CONTRACT (stable — the skill depends on this):
#   - LAST stdout line is ALWAYS exactly one of:
#       VERIFY: PASS <file> (<checker>)
#       VERIFY: FAIL <file> (<checker>) :: <first-error-line>
#       VERIFY: SKIP <file> (<reason>)      # unknown type / no checker -> not a failure
#   - exit 0 on PASS or SKIP, exit 1 on FAIL, exit 2 on usage error.
#   - checker auto-detected by extension, then shebang. Zero config, zero args
#     beyond the path. Missing tool for a known type -> SKIP (never a false FAIL).
#
# DETECT LADDER: .py -> py_compile ; .sh/.bash -> bash -n ; .js/.mjs/.cjs ->
#   node --check ; .ts/.tsx -> tsc --noEmit (fallback node --check off) ;
#   .json -> jq empty (fallback python json) ; shebang python/bash if no ext.
#   Anything else -> SKIP (checker=none). We deliberately do NOT run project
#   test suites here — that is the caller's job when it wants deeper proof; this
#   is the fast syntax/type gate that catches the 80% the model breaks.
#
# fail-open where it matters: a MISSING checker binary is SKIP not FAIL, so the
# loop never chases a phantom error caused by a toolless box. A real syntax
# error from a present checker is a true FAIL.

set -uo pipefail 2>/dev/null || true

f="${1:-}"
if [ -z "$f" ]; then
  echo "usage: verify.sh <file>" >&2
  echo "VERIFY: SKIP <none> (no-file-given)"
  exit 2
fi
if [ ! -f "$f" ]; then
  echo "VERIFY: FAIL $f (fs) :: file does not exist"
  exit 1
fi

# --- first-error extractor: newest stderr line that looks like an error -------
# WHY: checkers vary; we want ONE concise line for the model to act on. Prefer a
# line containing Error/error/SyntaxError/line N; else the last non-empty line.
_first_err() {
  # $1 = combined output file
  local out="$1" line
  line=$(grep -m1 -iE 'error|invalid|unexpected|cannot|expected' "$out" 2>/dev/null | head -1)
  [ -z "$line" ] && line=$(grep -vE '^[[:space:]]*$' "$out" 2>/dev/null | tail -1)
  # collapse whitespace, cap length so the verdict line stays one readable row
  printf '%s' "$line" | tr '\n\t' '  ' | cut -c1-240
}

_have() { command -v "$1" >/dev/null 2>&1; }

# --- checker resolution -------------------------------------------------------
ext="${f##*.}"
checker=""
case "$ext" in
  py)            checker="py_compile" ;;
  sh|bash)       checker="bash_n" ;;
  js|mjs|cjs)    checker="node_check" ;;
  ts|tsx)        checker="tsc" ;;
  json)          checker="json" ;;
  *)
    # no useful extension -> sniff shebang
    first=$(head -1 "$f" 2>/dev/null)
    case "$first" in
      *python*) checker="py_compile" ;;
      *bash*|*/sh) checker="bash_n" ;;
      *node*)   checker="node_check" ;;
      *)        checker="none" ;;
    esac
    ;;
esac

tmp=$(mktemp 2>/dev/null || echo "/tmp/verify.$$.out")
trap 'rm -f "$tmp" 2>/dev/null' EXIT

rc=0
case "$checker" in
  py_compile)
    if ! _have python3; then echo "VERIFY: SKIP $f (python3-missing)"; exit 0; fi
    python3 -m py_compile "$f" >"$tmp" 2>&1 || rc=$?
    ;;
  bash_n)
    if ! _have bash; then echo "VERIFY: SKIP $f (bash-missing)"; exit 0; fi
    bash -n "$f" >"$tmp" 2>&1 || rc=$?
    ;;
  node_check)
    if ! _have node; then echo "VERIFY: SKIP $f (node-missing)"; exit 0; fi
    node --check "$f" >"$tmp" 2>&1 || rc=$?
    ;;
  tsc)
    if _have tsc; then
      tsc --noEmit "$f" >"$tmp" 2>&1 || rc=$?
    elif _have node; then
      node --check "$f" >"$tmp" 2>&1 || rc=$?   # weaker fallback: syntax only
      checker="node_check(ts-fallback)"
    else
      echo "VERIFY: SKIP $f (tsc+node-missing)"; exit 0
    fi
    ;;
  json)
    if _have jq; then
      jq empty "$f" >"$tmp" 2>&1 || rc=$?
    elif _have python3; then
      python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$f" >"$tmp" 2>&1 || rc=$?
      checker="json(python)"
    else
      echo "VERIFY: SKIP $f (jq+python-missing)"; exit 0
    fi
    ;;
  none|"")
    echo "VERIFY: SKIP $f (no-checker-for-type)"
    exit 0
    ;;
esac

if [ "$rc" -eq 0 ]; then
  echo "VERIFY: PASS $f ($checker)"
  exit 0
else
  echo "VERIFY: FAIL $f ($checker) :: $(_first_err "$tmp")"
  exit 1
fi
