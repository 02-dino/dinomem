#!/usr/bin/env bash
# diagnose_test.sh — pins scripts/diagnose.sh's verdict contract.  [v1]
#
# WHY: the edit-verify-loop calls diagnose.sh for the RICHER error read; its
# contract (last line = DIAGNOSE: CLEAN|ISSUES|SKIP, exit 0/1/2) is what the
# loop parses. Pin it empirically — an exit code can't lie. Because the linter
# ladder degrades by box (ruff? shellcheck? tsc? maybe none), CLEAN-family cases
# accept CLEAN *or* SKIP (a toolless box legitimately SKIPs); only genuinely
# broken inputs must be ISSUES.

set -uo pipefail 2>/dev/null || true
HERE="$(cd "$(dirname "$0")" && pwd)"
DIAG="$HERE/../scripts/diagnose.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

pass=0 fail=0
_run() { out="$(bash "$DIAG" "$1" 2>&1)"; rc=$?; last="$(printf '%s\n' "$out" | tail -1)"; }
# accept a set of verdicts (space-separated) + expected exit set
_ck() { # $1 label  $2 "V1 V2.."  $3 "E1 E2.."
  local label="$1" verds="$2" exits="$3" okv=0 oke=0 v e
  for v in $verds; do printf '%s' "$last" | grep -q "DIAGNOSE: $v" && okv=1; done
  for e in $exits; do [ "$rc" -eq "$e" ] && oke=1; done
  if [ "$okv" -eq 1 ] && [ "$oke" -eq 1 ]; then
    echo "  ok   $label -> $last (exit=$rc)"; pass=$((pass+1))
  else
    echo "  FAIL $label -> want [$verds | exit $exits] got [$last exit=$rc]"; fail=$((fail+1))
  fi
}

# clean py -> CLEAN (or SKIP if no python at all)
printf 'x = 1\nprint(x)\n' > "$TMP/good.py"
_run "$TMP/good.py";   _ck "clean py"       "CLEAN SKIP" "0"
# broken py syntax -> ISSUES (py_compile rung always catches this)
printf 'def f(:\n  pass\n' > "$TMP/bad.py"
_run "$TMP/bad.py";    _ck "broken py"      "ISSUES"     "1"
# clean bash -> CLEAN or SKIP
printf '#!/usr/bin/env bash\necho hi\n' > "$TMP/good.sh"
_run "$TMP/good.sh";   _ck "clean sh"       "CLEAN SKIP" "0"
# broken bash -> ISSUES (bash -n rung catches; shellcheck also would)
printf '#!/usr/bin/env bash\nif then fi\n' > "$TMP/bad.sh"
_run "$TMP/bad.sh";    _ck "broken sh"      "ISSUES"     "1"
# broken json -> ISSUES
printf '{"a":1\n' > "$TMP/bad.json"
_run "$TMP/bad.json";  _ck "broken json"    "ISSUES"     "1"
# unknown type -> SKIP
printf 'plain text\n' > "$TMP/n.txt"
_run "$TMP/n.txt";     _ck "unknown type"   "SKIP"       "0"
# missing file -> ISSUES
_run "$TMP/nope.py";   _ck "missing file"   "ISSUES"     "1"
# shebang sniff, no ext, clean -> CLEAN or SKIP
printf '#!/usr/bin/env python3\nx=1\n' > "$TMP/noext"
_run "$TMP/noext";     _ck "shebang sniff"  "CLEAN SKIP" "0"

echo "---"
echo "diagnose_test: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
