#!/usr/bin/env bash
# config-redact.sh — emit a secret-masked copy of an OpenClaw config for snapshotting.
#
# WHY: openclaw.json is the highest-value file to keep undo-history on (one bad
# comma crash-loops the gateway), but it's dense with live secrets (apiKey /
# botToken / gateway auth token / env creds). Committing it raw every tick would
# turn the local snapshot store into a time-machine of every key. This writes a
# STRUCTURE-PRESERVING copy with secret VALUES masked ("***]") so config diffs
# stay meaningful (what key/route/model changed) while no credential ever enters
# git history.
#
# CONTRACT: config-redact.sh <src-config.json> <dst-snapshot.json>
#   - src must be valid JSON (invalid -> exit 1, dst untouched: never snapshot a
#     half-written/broken config as if it were a good restore point).
#   - dst is overwritten atomically (tmp + mv).
#   - Masking is by KEY NAME (case-insensitive substring), recursively, at any
#     depth, inside arrays too. A masked string becomes "***]"; non-string
#     secrets (rare) are left as-is (they're not credentials, e.g. maxTokens is
#     NOT matched — see NEGATIVE list).
# GOTCHA: match is on key name, not value. "maxTokens"/"keepRecentTokens" contain
#   "token" but are NOT secrets -> excluded via a negative-substring guard so we
#   don't mask numeric tuning knobs and lose their diff history.
set -u

SRC="${1:-}"; DST="${2:-}"
[ -n "$SRC" ] && [ -n "$DST" ] || { echo "usage: config-redact.sh <src> <dst>" >&2; exit 2; }
[ -f "$SRC" ] || { echo "config-redact: src not found: $SRC" >&2; exit 1; }
command -v jq >/dev/null 2>&1 || { echo "config-redact: jq required" >&2; exit 3; }

# Validate first — a broken config must NOT overwrite a good redacted snapshot.
jq empty "$SRC" 2>/dev/null || { echo "config-redact: src is not valid JSON, skipping: $SRC" >&2; exit 1; }

TMP="$(mktemp "${DST}.tmp.XXXXXX")" || exit 4
trap 'rm -f "$TMP"' EXIT

# walk(): recurse every object/array. Mask a string leaf iff its KEY name looks
# secret (positive substring) AND not a known non-secret (negative substring).
# Implemented in jq so structure/order/formatting stay identical to a real diff.
jq '
  def is_secret_key($k):
    ($k | ascii_downcase) as $lk
    | ([ "apikey","api_key","token","secret","password","passwd",
         "credential","privatekey","private_key","pat","authorization","cookie" ]
        | any(. as $s | $lk | contains($s)))
      and
      # NEGATIVE guard: numeric tuning knobs that merely contain "token"
      (([ "maxtokens","keeprecenttokens","softthresholdtokens","reservetokens",
          "tokenlimit","maxinputtokens","maxoutputtokens" ]
        | any(. as $n | $lk | contains($n))) | not);
  def redact:
    if type == "object" then
      with_entries(
        if (.value|type) == "string" and is_secret_key(.key)
        then .value = "***]"
        else .value |= redact end )
    elif type == "array" then map(redact)
    else . end;
  redact
' "$SRC" > "$TMP" 2>/dev/null || { echo "config-redact: jq transform failed: $SRC" >&2; exit 5; }

# Sanity: output must still be valid JSON and non-empty.
[ -s "$TMP" ] && jq empty "$TMP" 2>/dev/null || { echo "config-redact: produced invalid output, aborting: $SRC" >&2; exit 6; }

mv -f "$TMP" "$DST"
trap - EXIT
