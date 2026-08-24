#!/usr/bin/env bash
# repair-hint.sh — classify a failure into the next repair path. [v1]
#
# WHY: once verify/diagnose/test-target/dependency-check exist, the next failure
# mode is a BLIND retry loop: the model sees a red result but still has to infer
# whether it is syntax, lint, config, direct test, or dependent-test fallout.
# This helper keeps the categories coarse and machine-parseable so the loop can
# choose the next action faster and with less thrash.
#
# CONTRACT:
#   LAST stdout line is ALWAYS exactly:
#     REPAIR_HINT: <CATEGORY> :: <short-next-action>
#   Categories:
#     FS | SYNTAX | LINT | TEST | DEPENDENT_TEST | CONFIG | UNKNOWN
#   exit 0 on any classification, 2 on usage error.
#
# INPUT:
#   repair-hint.sh <source> <text...>
#   repair-hint.sh <source>    # reads stdin if no text args supplied
#
# SOURCE is one of: verify | diagnose | test | dependency-test
# Unknown source still classifies as best-effort via the text.

set -uo pipefail 2>/dev/null || true

src="${1:-}"
shift || true
if [ -z "$src" ]; then
  echo "usage: repair-hint.sh <source> <text...>" >&2
  echo "REPAIR_HINT: UNKNOWN :: no source provided; inspect raw output manually"
  exit 2
fi

if [ "$#" -gt 0 ]; then
  text="$*"
else
  text="$(cat 2>/dev/null || true)"
fi

emit() {
  echo "REPAIR_HINT: $1 :: $2"
  exit 0
}

low="$(printf '%s' "$text" | tr '[:upper:]' '[:lower:]')"

[ -n "$low" ] || emit "UNKNOWN" "no error text supplied; inspect the failing command output directly"

case "$low" in
  *"file does not exist"*|*"no such file or directory"*|*"(fs)"*)
    emit "FS" "fix the file/path issue first, then rerun the same check"
    ;;
esac

case "$low" in
  *"jq: parse error"*|*"unfinished json"*|*"expecting value"*|*"invalid json"*|*"json.load"*|*"jsondecodeerror"*|*"broken json"*|*"(jq)"*|*"json(py)"*)
    emit "CONFIG" "fix the config/data parse issue before retrying broader checks"
    ;;
esac

case "$src" in
  test)
    emit "TEST" "the direct test failed; inspect the failing test output and behavior path, not just syntax"
    ;;
  dependency-test)
    emit "DEPENDENT_TEST" "a dependent test failed; inspect the caller/importer path that fan-out selected"
    ;;
esac

case "$low" in
  *"syntaxerror"*|*"invalid syntax"*|*"unexpected token"*|*"expected a parameter"*|*"syntax error near unexpected token"*|*"parse error"*|*"py_compile"*|*"bash_n"*|*"node_check"*|*"tsc"*|*"unterminated"*|*"expected ')"*|*"expected ']'"*)
    emit "SYNTAX" "fix the parser/compiler error in the edited file before any deeper proof"
    ;;
esac

case "$src" in
  diagnose)
    emit "LINT" "fix the linter-reported issue in the edited file, then rerun diagnose and the narrow proof"
    ;;
  verify)
    emit "SYNTAX" "verify failed without a clearer pattern; treat it as a direct edited-file gate failure first"
    ;;
esac

case "$low" in
  *"ruff"*|*"pyflakes"*|*"shellcheck"*|*"undefined name"*|*"not defined"*|*"unused import"*|*"finding(s); first:"*|*"f401"*|*"f821"*|*"e999"*)
    emit "LINT" "fix the linter-reported issue in the edited file, then rerun diagnose and the narrow proof"
    ;;
  *"failed"*|*"assert"*|*"traceback"*|*"exception"*)
    emit "TEST" "inspect the failing test output and execution path; the issue is beyond the syntax gate"
    ;;
esac

emit "UNKNOWN" "failure type unclear; inspect the raw output manually before another fix attempt"
