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

# _ck LABEL PATTERN — fail if PATTERN (ERE, case-insensitive) appears in any candidate.
_ck() {
  local label="$1" pat="$2" hits
  hits="$(_candidates | xargs -0 grep -nIiE "$pat" 2>/dev/null \
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
