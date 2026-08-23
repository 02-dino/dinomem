#!/usr/bin/env bash
# doctor.sh — dinomem TEI embedding server health check.
#
# Checks the TEI (text-embeddings-inference) embed server dinomem depends on
# for memory extraction/review/search. Reports OK/unhealthy + basic model info.
# Zero deps beyond curl (already required by the rest of dinomem's tooling).
#
# Usage:
#   bash scripts/doctor.sh [--url URL] [--quiet]
#
# Options:
#   --url URL   Base URL of the TEI embed server (default: $DINOMEM_EMBED_URL's
#               host, or http://localhost:8080). Accepts either a bare base
#               (http://localhost:8080) or the full /v1/embeddings URL — the
#               /health and /info suffixes are derived automatically.
#   --quiet     Suppress narrative output; only print PASS/FAIL + exit code.
#
# Exit codes:
#   0  TEI server reachable and healthy
#   1  TEI server unreachable or reporting unhealthy
#
# Called manually for troubleshooting, or wired into install.sh's post-install
# "Next steps" output as the documented health-check command.
set -euo pipefail

QUIET=0
BASE_URL=""

while [ $# -gt 0 ]; do
  case "$1" in
    --url)    BASE_URL="$2"; shift 2 ;;
    --quiet)  QUIET=1; shift ;;
    -h|--help) grep -E '^#( |$)' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

# Resolve base URL: --url > DINOMEM_EMBED_URL (strip /v1/embeddings suffix) > default.
if [ -z "$BASE_URL" ]; then
  if [ -n "${DINOMEM_EMBED_URL:-}" ]; then
    BASE_URL="${DINOMEM_EMBED_URL%/v1/embeddings}"
    BASE_URL="${BASE_URL%/}"
  else
    BASE_URL="http://localhost:8080"
  fi
fi
BASE_URL="${BASE_URL%/}"

say() { [ "$QUIET" = 1 ] || echo "$@"; }

say "dinomem doctor — TEI embed server check"
say "  target: $BASE_URL"
say ""

HEALTH_URL="$BASE_URL/health"
INFO_URL="$BASE_URL/info"

FAIL=0

HEALTH_OUT="$(curl -sS -m 5 -w '\n%{http_code}' "$HEALTH_URL" 2>/dev/null || true)"
HEALTH_CODE="$(echo "$HEALTH_OUT" | tail -n1)"

if [ "$HEALTH_CODE" = "200" ]; then
  say "  [ok]   /health -> 200"
else
  say "  [fail] /health -> ${HEALTH_CODE:-unreachable}"
  say "         Server not responding. Common causes:"
  say "           - Container not running: docker ps | grep tei-embed"
  say "           - Wrong port/URL: check DINOMEM_EMBED_URL or --url"
  say "           - Just started: TEI takes ~30-60s to load the model on first boot"
  say "         Logs: docker logs tei-embed"
  FAIL=1
fi

if [ "$FAIL" = 0 ]; then
  INFO_OUT="$(curl -sS -m 5 "$INFO_URL" 2>/dev/null || true)"
  if [ -n "$INFO_OUT" ]; then
    MODEL_ID="$(echo "$INFO_OUT" | grep -o '"model_id":"[^"]*"' | head -1 | cut -d'"' -f4)"
    MAX_LEN="$(echo "$INFO_OUT" | grep -o '"max_input_length":[0-9]*' | head -1 | cut -d: -f2)"
    if [ -n "$MODEL_ID" ]; then
      say "  [ok]   model_id: $MODEL_ID"
    else
      say "  [warn] /info reachable but model_id not found in response"
    fi
    [ -n "$MAX_LEN" ] && say "  [ok]   max_input_length: $MAX_LEN"
  else
    say "  [warn] /info did not respond (non-fatal — /health already passed)"
  fi
fi

# ── Restart=on-failure check ─────────────────────────────────────────────────
_restart_fail=0
while IFS= read -r _unit; do
  _frag=$(systemctl show "$_unit" -p FragmentPath 2>/dev/null | cut -d= -f2)
  [ -z "$_frag" ] || [ ! -f "$_frag" ] && continue
  if ! grep -q "^Restart=on-failure" "$_frag" 2>/dev/null; then
    say "  [warn] $_unit missing Restart=on-failure in $_frag"
    say "         Run: bash <dinomem-base-dir>/scripts/install.sh to re-apply"
    _restart_fail=1
  fi
done < <(systemctl list-unit-files --type=service --plain --no-legend 2>/dev/null | grep "^openclaw" | grep -v "autosnapshot\|autocommit" | awk '{print $1}')
[ "$_restart_fail" = 0 ] && say "  [ok]   all openclaw units have Restart=on-failure"

# ── External watchdog check ───────────────────────────────────────────────────
if [ -z "${DINOMEM_WATCHDOG_CONFIGURED:-}" ]; then
  say "  [warn] External watchdog not configured. Gateway hangs will cause multi-hour outages."
  say "         Setup: <dinomem-base-dir>/docs/watchdog/README.md"
  say "         After setup: export DINOMEM_WATCHDOG_CONFIGURED=1 in gateway env"
else
  say "  [ok]   DINOMEM_WATCHDOG_CONFIGURED is set"
fi

say ""
if [ "$FAIL" = 0 ]; then
  say "RESULT: PASS — TEI embed server is healthy."
  exit 0
else
  say "RESULT: FAIL — TEI embed server is unreachable or unhealthy."
  exit 1
fi
