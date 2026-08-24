#!/usr/bin/env bash
# blast_radius_test.sh — pins scripts/blast-radius.sh pre-edit scope contract. [v1]
#
# WHY: blast-radius.sh decides whether the agent starts with a one-file patch,
# widens immediately, or stops to inspect first. If this drifts, autonomy gets
# sloppier before the first edit. Pin only the coarse categories.

set -uo pipefail 2>/dev/null || true
HERE="$(cd "$(dirname "$0")" && pwd)"
PICK="$HERE/../scripts/blast-radius.sh"
REPO="$(cd "$HERE/.." && pwd)"

pass=0 fail=0
_run() { out="$(bash "$PICK" "$1" 2>&1)"; rc=$?; last="$(printf '%s\n' "$out" | tail -1)"; }
_ck() {
  local label="$1" want="$2" ex="$3"
  if printf '%s' "$last" | grep -Fq "$want" && [ "$rc" -eq "$ex" ]; then
    echo "  ok   $label -> $last (exit=$rc)"; pass=$((pass+1))
  else
    echo "  FAIL $label -> want [$want exit=$ex] got [$last exit=$rc]"; fail=$((fail+1))
  fi
}

_run "$REPO/scripts/verify.sh"
_ck 'single-file exact sibling proof' 'BLAST_RADIUS: SINGLE_FILE ::' 0

_run "$REPO/scripts/lib/gate_lib.sh"
_ck 'shared helper direct proof is not enough' 'BLAST_RADIUS: STOP_AND_INSPECT ::' 0

_run "$REPO/scripts/install.sh"
_ck 'shared load-bearing no narrow proof' 'BLAST_RADIUS: STOP_AND_INSPECT ::' 0

_run "$REPO/hooks/dinomem-open-notes/HOOK.md"
_ck 'hook doc without narrow proof' 'BLAST_RADIUS: STOP_AND_INSPECT ::' 0

_run "$REPO/scripts/nope.sh"
_ck 'missing file' 'BLAST_RADIUS: UNKNOWN ::' 0

echo '---'
echo "blast_radius_test: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
