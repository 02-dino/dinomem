#!/usr/bin/env bash
# vertical_neutrality_test.sh — pins the repo's VERTICAL NEUTRALITY contract. [v1]
#
# WHY: this engine ships to every kind of agent (sales, support, events, content,
# markets). Measured 2026-10-29 on a 12-agent box: a crypto/market-analyst word
# list inside generate_topic_index.py's fallback classifier and inside
# memory_review.py's prompt persona leaked a [MARKET] label into 7 non-analyst
# workspaces (konsultan: 50/50 rows) and — worse — graded their memory for
# DELETION against "is this useful to a market analyst". Both files are tracked
# and ship in the buyer release, so the bias travelled with every copy.
#
# The PR template already asks for "no hardcoded paths"; nothing enforced it.
# This is the enforcement. It fails LOUD on the two bug classes that actually
# shipped:
#   1. vertical vocabulary in a surface that STEERS behavior (an LLM prompt or a
#      classifier branch)
#   2. an absolute /root/.openclaw/workspace-<agent> path baked into a shipped file
#
# DELIBERATELY NOT FLAGGED (these are fine, and flagging them trains people to
# ignore this test): *_test.py / selftest.mjs fixtures and benchmark/ data, where
# "BTC"/"ETH" are arbitrary opaque tokens with no behavioral meaning; and prose
# in README/BEFORE_AFTER/docs that describes history or positioning.
set -uo pipefail 2>/dev/null || true

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
pass=0 fail=0

# Runtime surfaces only — the files that actually execute or get fed to an LLM.
SURFACES=(procedures tools skills hooks plugins lib scripts templates)

# Collect candidate files once (NUL-safe), applying the fixture/doc carve-outs.
_candidates() {
  local d
  for d in "${SURFACES[@]}"; do
    [ -d "$REPO/$d" ] || continue
    find "$REPO/$d" -type f \
      \( -name '*.py' -o -name '*.sh' -o -name '*.md' -o -name '*.mjs' -o -name '*.ts' \) \
      ! -name '*_test.py' ! -name '*_test.sh' ! -name 'selftest.mjs' \
      ! -name '*.bak*' ! -path '*/__pycache__/*' -print0 2>/dev/null
  done
}

# SANITY GATE: a scan of zero files reports "5 passed" and exits 0 — a green that
# means "nothing was checked", not "nothing is wrong". That is the exact
# false-confidence class this test exists to kill, so it must not be possible
# here. Trips if the surface dirs get renamed/moved, if the file is run from
# outside the repo, or if a future refactor breaks _candidates.
_candidate_count="$(_candidates | tr -dc '\0' | wc -c | tr -d ' ')"
if [ "${_candidate_count:-0}" -eq 0 ]; then
  echo "vertical_neutrality_test: ABORT — 0 candidate files under $REPO" >&2
  echo "  expected runtime surfaces (${SURFACES[*]}) to contain scannable files." >&2
  echo "  refusing to report a pass on an unscanned repo." >&2
  exit 2
fi

# _ck LABEL PATTERN — fail if PATTERN (ERE, case-insensitive) appears in any candidate.
# -r: with zero candidates, xargs must NOT run grep at all — a bare grep would
#     read STDIN and either hang or silently "pass" an unscanned repo.
# -H: force the filename prefix; grep omits it on a single-file batch, which
#     would print a bare "12:<line>" with no path in the FAIL report.
_ck() {
  local label="$1" pat="$2" hits
  hits="$(_candidates | xargs -0r grep -nHIiE "$pat" 2>/dev/null \
          | grep -v 'vertical_neutrality' || true)"
  if [ -z "$hits" ]; then
    echo "  ok   $label"; pass=$((pass+1))
  else
    echo "  FAIL $label — vertical bias in a behavior-steering surface:"
    printf '%s\n' "$hits" | sed 's|^'"$REPO"'/|         |' | head -12
    fail=$((fail+1))
  fi
}

echo "vertical_neutrality_test: scanning $REPO"

# 1. Prompt persona / relevance bar locked to one industry.
_ck 'no market-analyst persona in any prompt' 'market analyst'

# 2. The classifier + prompt rules that actually mislabelled 7 workspaces.
_ck 'no TICKERS-FIRST prompt rule'            'tickers first'
_ck 'no crypto ticker list in a prompt/branch' "['\"]btc['\"]|['\"]eth['\"]|BTC, ETH"

# 3. Domain-specific tool/jargon vocabulary in shipped runtime surfaces.
_ck 'no crypto-native jargon'                 'rugcheck|hyperliquid|memecoin|orderbook|funding rate'

# 4. The hardcoded-path class the PR template already forbids. Absolute paths
#    only: a bare word like "workspace-analyst" inside an explanatory comment is
#    documentation, not a baked path.
_ck 'no absolute workspace path baked in'     '/root/\.openclaw/workspace-[a-z0-9]+'

echo '---'
echo "vertical_neutrality_test: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
