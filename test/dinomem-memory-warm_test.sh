#!/usr/bin/env bash
# dinomem-memory-warm_test.sh — pins the startup-warm hook's contract. [v1]
#
# WHY: the hook fires on gateway:startup and must stay SILENT-SAFE — a bad edit
# here either blocks boot or (worse) quietly warms nothing, which looks fine in
# logs while every first query still pays the cold cost.
#
# Two regressions this pins specifically:
#  1. TEI model resolution read `asString(defaults.memorySearch)` — a RECORD
#     handed to a string guard, so it was ALWAYS undefined and step 2 silently
#     fell through. Fixed 2026-10-04 to read remote.model / memorySearch.model /
#     defaults.model. Caught only because the resolver was read line by line.
#  2. Warming used to be opt-in via DINOMEM_WARM_AGENTS. It is now mandatory
#     from cfg.agents.list; a reintroduced env gate would make warming a no-op
#     on every normal install.

set -uo pipefail 2>/dev/null || true
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
H="$REPO/hooks/dinomem-memory-warm/handler.ts"
DOC="$REPO/hooks/dinomem-memory-warm/HOOK.md"

pass=0 fail=0
_ck() {
  local label="$1" ok="$2"
  if [ "$ok" = 1 ]; then
    echo "  ok   $label"; pass=$((pass+1))
  else
    echo "  FAIL $label"; fail=$((fail+1))
  fi
}
_has()  { grep -Fq "$2" "$1" && echo 1 || echo 0; }
_lacks(){ grep -Fq "$2" "$1" && echo 0 || echo 1; }

# ── file presence ────────────────────────────────────────────────────────────
_ck 'handler.ts exists'  "$([ -f "$H" ] && echo 1 || echo 0)"
_ck 'HOOK.md exists'     "$([ -f "$DOC" ] && echo 1 || echo 0)"
[ -f "$H" ] || { echo '---'; echo "dinomem-memory-warm_test: $pass passed, $((fail+1)) failed"; exit 1; }

# ── syntax: must parse as TS via node's type-stripper (no build step) ────────
if node --experimental-strip-types --check "$H" >/dev/null 2>&1; then
  _ck 'handler.ts parses (node --experimental-strip-types --check)' 1
else
  _ck 'handler.ts parses (node --experimental-strip-types --check)' 0
fi

# ── regression 1: model must NOT be read off the memorySearch record ─────────
# Comments are allowed to mention it (the fix is documented there), so compare
# against code lines only.
code_only="$(grep -vE '^\s*(//|\*|/\*)' "$H")"
if printf '%s' "$code_only" | grep -Fq 'asString(defaults.memorySearch)'; then
  _ck 'model is NOT resolved from the memorySearch record (type-mismatch bug)' 0
else
  _ck 'model is NOT resolved from the memorySearch record (type-mismatch bug)' 1
fi
_ck 'model falls back remote.model -> memorySearch.model -> defaults.model' \
    "$(_has "$H" 'asString(remote.model)')"

# ── regression 2: warming is mandatory, not env-gated ───────────────────────
_ck 'DINOMEM_WARM_AGENTS opt-in gate is gone'      "$(_lacks "$H" 'DINOMEM_WARM_AGENTS')"
_ck 'agents resolved from cfg.agents.list'         "$(_has "$H" 'agentsCfg.list')"
_ck 'HOOK.md no longer documents the opt-in gate'  "$(_lacks "$DOC" 'DINOMEM_WARM_AGENTS')"

# ── contract: fires only on gateway:startup ─────────────────────────────────
_ck 'guards on gateway:startup'  "$(_has "$H" 'gateway:startup')"

# ── contract: never blocks boot ────────────────────────────────────────────
_ck 'child processes detached'   "$(_has "$H" 'detached: true')"
_ck 'child unref()ed'            "$(_has "$H" 'child.unref()')"
_ck 'TEI warm not awaited'       "$(_has "$H" 'warmTei(cfg).catch(')"
_ck 'handler body wrapped in try' "$(_has "$H" 'catch (err)')"

# ── contract: TEI warm hits the embeddings path with an env override ────────
_ck 'posts to /embeddings'        "$(_has "$H" '/embeddings')"
_ck 'DINOMEM_TEI_URL override'    "$(_has "$H" 'DINOMEM_TEI_URL')"
_ck 'DINOMEM_TEI_MODEL override'  "$(_has "$H" 'DINOMEM_TEI_MODEL')"

# ── behaviour: resolver returns undefined when nothing resolves (fail-open) ──
# Exercise the real resolver by stubbing a config with no usable embedding info.
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
cat > "$tmp/probe.ts" <<'TS'
// Import the handler for its side-effect-free default export, then drive it
// with an event whose cfg resolves to nothing. It must return without throwing.
import handler from "HANDLER_PATH";
const ev = { type: "gateway:startup", context: { cfg: { agents: { list: [] } } } };
await handler(ev as never);
console.log("FAILOPEN_OK");
TS
sed -i "s#HANDLER_PATH#$H#" "$tmp/probe.ts"
if out="$(cd "$tmp" && node --experimental-strip-types probe.ts 2>&1)" \
   && printf '%s' "$out" | grep -Fq 'FAILOPEN_OK'; then
  _ck 'empty agent list + no TEI config -> returns quietly (fail-open)' 1
else
  _ck 'empty agent list + no TEI config -> returns quietly (fail-open)' 0
  printf '%s\n' "$out" | tail -3 | sed 's/^/        /'
fi

echo '---'
echo "dinomem-memory-warm_test: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
