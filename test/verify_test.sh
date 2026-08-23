#!/usr/bin/env bash
# verify_test.sh — proves scripts/verify.sh honors its verdict contract.  [v1]
#
# WHY: verify.sh is the load-bearing gate of the edit-verify-loop. Its contract
# (last line = VERIFY: PASS|FAIL|SKIP, exit 0/1/2) is what the skill loops on. A
# drift in that contract silently breaks the loop, so pin it empirically here —
# an exit code can't lie the way a comment can. Covers BOTH paths + edges.

set -uo pipefail 2>/dev/null || true
HERE="$(cd "$(dirname "$0")" && pwd)"
VERIFY="$HERE/../scripts/verify.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

pass=0 fail=0
_ck() { # $1=label $2=expect_verdict $3=expect_exit  (reads $out/$rc)
  local label="$1" ev="$2" ex="$3" last
  last=$(printf '%s\n' "$out" | tail -1)
  if printf '%s' "$last" | grep -q "VERIFY: $ev" && [ "$rc" -eq "$ex" ]; then
    echo "  ok   $label -> $ev exit=$rc"; pass=$((pass+1))
  else
    echo "  FAIL $label -> want [$ev exit=$ex] got [$last exit=$rc]"; fail=$((fail+1))
  fi
}
_run() { out="$(bash "$VERIFY" "$1" 2>&1)"; rc=$?; }

# --- PASS path: valid python ---
printf 'x = 1\nprint(x)\n' > "$TMP/good.py"
_run "$TMP/good.py";        _ck "valid py"        "PASS" 0

# --- FAIL path: broken python syntax ---
printf 'def f(:\n  pass\n' > "$TMP/bad.py"
_run "$TMP/bad.py";         _ck "broken py"       "FAIL" 1

# --- PASS path: valid bash ---
printf '#!/usr/bin/env bash\necho hi\n' > "$TMP/good.sh"
_run "$TMP/good.sh";        _ck "valid sh"        "PASS" 0

# --- FAIL path: broken bash ---
printf '#!/usr/bin/env bash\nif then fi\n' > "$TMP/bad.sh"
_run "$TMP/bad.sh";         _ck "broken sh"       "FAIL" 1

# --- PASS path: valid json (needs jq or python3) ---
printf '{"a":1}\n' > "$TMP/good.json"
_run "$TMP/good.json";      _ck "valid json"      "PASS" 0

# --- FAIL path: broken json ---
printf '{"a":1\n' > "$TMP/bad.json"
_run "$TMP/bad.json";       _ck "broken json"     "FAIL" 1

# --- EDGE: unknown type -> SKIP (not FAIL) ---
printf 'hello world\n' > "$TMP/notes.txt"
_run "$TMP/notes.txt";      _ck "unknown type"    "SKIP" 0

# --- EDGE: missing file -> FAIL ---
_run "$TMP/does_not_exist.py"; _ck "missing file" "FAIL" 1

# --- EDGE: shebang sniff with no extension ---
printf '#!/usr/bin/env python3\nx=1\n' > "$TMP/scriptnoext"
_run "$TMP/scriptnoext";    _ck "shebang sniff py" "PASS" 0

echo "---"
echo "verify_test: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
