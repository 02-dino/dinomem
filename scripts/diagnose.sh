#!/usr/bin/env bash
# diagnose.sh — deeper on-demand code diagnostics for the edit-verify-loop.  [v1]
#
# WHY (the frame): verify.sh is the FAST syntax/parse gate (py_compile/bash -n).
# diagnose.sh is the RICHER read the model calls WHEN it wants real linter signal
# — undefined names, unused imports, type errors, style/logic smells — not just
# "does it parse". It is NOT injected passively anywhere; the edit-verify-loop
# invokes it only when a green syntax gate isn't enough (behavior change, "why is
# this wrong when it parses"). Zero passive cost.
#
# CONTRACT (stable — callers parse the LAST stdout line):
#   - LAST line is ALWAYS one of:
#       DIAGNOSE: CLEAN <file> (<tool>)
#       DIAGNOSE: ISSUES <file> (<tool>) :: <n> finding(s); first: <first-line>
#       DIAGNOSE: SKIP <file> (<reason>)     # unknown type / no tool -> not a failure
#   - exit 0 on CLEAN or SKIP, 1 on ISSUES, 2 on usage error.
#   - Full findings printed ABOVE the verdict line (the model reads them to fix).
#
# TOOL LADDER (best available wins; MISSING TOOL = graceful degrade, never FAIL):
#   .py       -> ruff check  ||  pyflakes  ||  python -m py_compile   (SKIP if none)
#   .sh/.bash -> shellcheck  ||  bash -n
#   .js/.ts   -> tsc --noEmit ||  node --check
#   .json     -> jq empty    ||  python json
#   unknown   -> SKIP
#
# fail-open: a missing linter degrades to the next rung, and if even the syntax
# rung is absent we SKIP (CLEAN-ish, exit 0) — the loop must NEVER chase a
# phantom "issue" that is really just a toolless box.

set -uo pipefail 2>/dev/null || true

f="${1:-}"
if [ -z "$f" ]; then
  echo "usage: diagnose.sh <file>" >&2
  echo "DIAGNOSE: SKIP <none> (no-file-given)"
  exit 2
fi
if [ ! -f "$f" ]; then
  echo "DIAGNOSE: ISSUES $f (fs) :: 1 finding(s); first: file does not exist"
  exit 1
fi

_have() { command -v "$1" >/dev/null 2>&1; }

# first meaningful line of a findings file, whitespace-collapsed + capped
_first() {
  grep -vE '^[[:space:]]*$' "$1" 2>/dev/null | head -1 | tr '\n\t' '  ' | cut -c1-200
}
# rough finding count: non-empty lines (good enough for a hint, not a metric)
_count() { grep -cvE '^[[:space:]]*$' "$1" 2>/dev/null || echo 0; }

tmp=$(mktemp 2>/dev/null || echo "/tmp/diagnose.$$.out")
trap 'rm -f "$tmp" 2>/dev/null' EXIT

ext="${f##*.}"
# shebang sniff for extension-less scripts
if [ "$ext" = "$f" ]; then
  case "$(head -1 "$f" 2>/dev/null)" in
    *python*) ext="py" ;;
    *bash*|*/sh) ext="sh" ;;
    *node*)   ext="js" ;;
  esac
fi

rc=0 tool=""
case "$ext" in
  py)
    if _have ruff;    then tool="ruff";     ruff check "$f" >"$tmp" 2>&1 || rc=$?
    elif _have pyflakes; then tool="pyflakes"; pyflakes "$f" >"$tmp" 2>&1 || rc=$?
    elif _have python3;  then tool="py_compile"; python3 -m py_compile "$f" >"$tmp" 2>&1 || rc=$?
    else echo "DIAGNOSE: SKIP $f (no-python-linter)"; exit 0; fi
    ;;
  sh|bash)
    if _have shellcheck; then tool="shellcheck"; shellcheck "$f" >"$tmp" 2>&1 || rc=$?
    elif _have bash;     then tool="bash_n";     bash -n "$f" >"$tmp" 2>&1 || rc=$?
    else echo "DIAGNOSE: SKIP $f (no-sh-linter)"; exit 0; fi
    ;;
  js|mjs|cjs|ts|tsx)
    if _have tsc;   then tool="tsc";        tsc --noEmit "$f" >"$tmp" 2>&1 || rc=$?
    elif _have node; then tool="node_check"; node --check "$f" >"$tmp" 2>&1 || rc=$?
    else echo "DIAGNOSE: SKIP $f (no-js-linter)"; exit 0; fi
    ;;
  json)
    if _have jq;        then tool="jq";     jq empty "$f" >"$tmp" 2>&1 || rc=$?
    elif _have python3; then tool="json(py)"; python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$f" >"$tmp" 2>&1 || rc=$?
    else echo "DIAGNOSE: SKIP $f (no-json-tool)"; exit 0; fi
    ;;
  *)
    echo "DIAGNOSE: SKIP $f (no-linter-for-type)"; exit 0
    ;;
esac

if [ "$rc" -eq 0 ]; then
  echo "DIAGNOSE: CLEAN $f ($tool)"
  exit 0
else
  cat "$tmp"   # full findings ABOVE the verdict, for the model to fix
  echo "DIAGNOSE: ISSUES $f ($tool) :: $(_count "$tmp") finding(s); first: $(_first "$tmp")"
  exit 1
fi
