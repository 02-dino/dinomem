#!/usr/bin/env bash
# dependency_check_test.sh — pins scripts/dependency-check.sh contract. [v1]
#
# WHY: dependency-check adds the next autonomy layer after test-target: if a
# shared helper changed, find immediate dependents and run only their narrowest
# tests. This must stay base-safe (NONE without graph) and non-guessy (dedup,
# skip dependents with no narrow proof).

set -uo pipefail 2>/dev/null || true
HERE="$(cd "$(dirname "$0")" && pwd)"
CHECK="$HERE/../scripts/dependency-check.sh"

pass=*** fail=0
_run() { out="$($1 "$2" 2>&1)"; rc=$?; last="$(printf '%s\n' "$out" | tail -1)"; }
_ck() {
  local label="$1" want="$2" ex="$3"
  if printf '%s' "$last" | grep -Fq "$want" && [ "$rc" -eq "$ex" ]; then
    echo "  ok   $label -> $last (exit=$rc)"; pass=***
  else
    echo "  FAIL $label -> want [$want exit=$ex] got [$last exit=$rc]"; fail=$((fail+1))
  fi
}

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
REPO="$TMP/repo"
mkdir -p "$REPO/scripts/lib" "$REPO/test"
printf '#!/usr/bin/env bash\n' > "$REPO/scripts/lib/shared.sh"
printf '#!/usr/bin/env bash\n' > "$REPO/scripts/caller_a.sh"
printf '#!/usr/bin/env bash\n' > "$REPO/scripts/caller_b.sh"
printf '#!/usr/bin/env bash\n' > "$REPO/scripts/no_test.sh"
printf '#!/usr/bin/env bash\n' > "$REPO/test/caller_a_test.sh"
printf '#!/usr/bin/env bash\n' > "$REPO/test/caller_b_test.sh"
chmod +x "$REPO"/scripts/*.sh "$REPO/scripts/lib/shared.sh" "$REPO"/test/*.sh

cat > "$REPO/scripts/test-target.sh" <<'EOF'
#!/usr/bin/env bash
f="$1"
case "$f" in
  */scripts/caller_a.sh) echo 'TEST_TARGET: bash test/caller_a_test.sh'; exit 0 ;;
  */scripts/caller_b.sh) echo 'TEST_TARGET: bash test/caller_b_test.sh'; exit 0 ;;
  *) echo 'TEST_TARGET: NONE'; exit 1 ;;
esac
EOF
chmod +x "$REPO/scripts/test-target.sh"

cat > "$TMP/mock_code_query.py" <<'PY'
#!/usr/bin/env python3
import json, sys
verb = sys.argv[1]
sym = sys.argv[2]
if verb != 'explain':
    print(json.dumps({'ok': False}))
    raise SystemExit(0)
if sym == 'shared':
    print(json.dumps({
        'ok': True,
        'node': 'file shared.sh «repo»  (workspace/repo/scripts/lib/shared.sh:1)',
        'outgoing': {'contains': [
            {'target': 'function helper_a «repo»  (workspace/repo/scripts/lib/shared.sh:10)'},
            {'target': 'function helper_b «repo»  (workspace/repo/scripts/lib/shared.sh:20)'}
        ]},
        'called_by': []
    }))
elif sym == 'helper_a':
    print(json.dumps({
        'ok': True,
        'symbol': sym,
        'called_by': [
            {'source': 'file caller_a.sh «repo»  (workspace/repo/scripts/caller_a.sh:1)'},
            {'source': 'function wrap_a «repo»  (workspace/repo/scripts/caller_a.sh:8)'}
        ]
    }))
elif sym == 'helper_b':
    print(json.dumps({
        'ok': True,
        'symbol': sym,
        'called_by': [
            {'source': 'file caller_b.sh «repo»  (workspace/repo/scripts/caller_b.sh:1)'},
            {'source': 'file no_test.sh «repo»  (workspace/repo/scripts/no_test.sh:1)'}
        ]
    }))
else:
    print(json.dumps({'ok': True, 'symbol': sym, 'called_by': []}))
PY
chmod +x "$TMP/mock_code_query.py"

# base-safe fallback: no graph helper discoverable -> NONE
_run "env -u OPENCLAW_WORKSPACE -u DINOMEM_WORKSPACE bash $CHECK" "$REPO/scripts/lib/shared.sh"
_ck "no graph helper" "DEPENDENCY_CHECK: NONE" 1

# graph-driven dependent selection: dedupe repeated caller_a hits, skip no_test
_run "env DINOMEM_CODE_QUERY=$TMP/mock_code_query.py bash $CHECK" "$REPO/scripts/lib/shared.sh"
_ck "dedup + narrow proofs" "DEPENDENCY_CHECK: bash test/caller_a_test.sh || bash test/caller_b_test.sh" 0

# missing file -> NONE
_run "env DINOMEM_CODE_QUERY=$TMP/mock_code_query.py bash $CHECK" "$REPO/scripts/lib/absent.sh"
_ck "missing file" "DEPENDENCY_CHECK: NONE" 1

echo "---"
echo "dependency_check_test: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
