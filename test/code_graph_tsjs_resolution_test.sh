#!/usr/bin/env bash
# code_graph_tsjs_resolution_test.sh — deterministic test for the JS/TS
# resolution layer: tsconfig `paths` aliases (#1a), barrel re-export hop + cycle
# guard (#1b), and module-scoped member calls (#3) with name-collision + unscoped
# drop. Generic resolver (${DINOMEM_WORKSPACE} OR ~/.openclaw/workspace-*);
# SKIPs if the javascript/typescript grammar is absent.
set -u

CG=""
_cands=()
[ -n "${DINOMEM_WORKSPACE:-}" ] && _cands+=("${DINOMEM_WORKSPACE}/procedures/code_graph.py")
_here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_cands+=("${_here}/../procedures/code_graph.py")
for d in "$HOME"/.openclaw/workspace-*; do
  _cands+=("$d/procedures/code_graph.py")
done
for c in "${_cands[@]}"; do
  if [ -f "$c" ]; then CG="$c"; break; fi
done
if [ -z "$CG" ]; then
  echo "SKIP: code_graph.py not found"
  exit 0
fi

if ! python3 -c "from tree_sitter_language_pack import get_parser; get_parser('javascript'); get_parser('typescript')" 2>/dev/null; then
  echo "SKIP: javascript/typescript tree-sitter grammar not available"
  exit 0
fi

FIX="$(mktemp -d)"
trap 'rm -rf "$FIX"' EXIT

# ── #1a: tsconfig paths alias ────────────────────────────────────────────────
mkdir -p "$FIX/src" "$FIX/packages/lib"
cat > "$FIX/tsconfig.json" <<'EOF'
{
  // JSONC: comment + trailing comma tolerated
  "compilerOptions": {
    "baseUrl": ".",
    "paths": { "@/*": ["src/*"], "@lib/*": ["packages/lib/*"] },
  },
}
EOF
echo 'export function helper(x){return x+1;}' > "$FIX/src/util.ts"
echo 'export function libFn(){return 1;}'     > "$FIX/packages/lib/thing.ts"
cat > "$FIX/app.ts" <<'EOF'
import { helper } from "@/util";
import { libFn } from "@lib/thing";
export function app() { return helper(2) + libFn(); }
EOF

# ── #1b: barrel re-export hop + cycle ────────────────────────────────────────
echo 'export function thing(){return 1;}' > "$FIX/y.ts"
echo 'export * from "./y";'               > "$FIX/barrel.ts"
cat > "$FIX/useBarrel.ts" <<'EOF'
import { thing } from "./barrel";
export function ub() { return thing(); }
EOF
echo 'export * from "./cycleB";' > "$FIX/cycleA.ts"
echo 'export * from "./cycleA";' > "$FIX/cycleB.ts"
echo 'import { z } from "./cycleA";' > "$FIX/useCycle.ts"

# ── #3: member-call scoping + collision + unscoped drop ──────────────────────
echo 'export function collided(x){return x+1;}' > "$FIX/modA.ts"
echo 'export function collided(y){return y-1;}' > "$FIX/modB.ts"
cat > "$FIX/member.ts" <<'EOF'
import * as a from "./modA";
import { collided as bc } from "./modB";
export function m(arr) {
  a.collided(1);   // -> must resolve to modA.ts, NOT modB.ts
  arr.map(z => z); // unscoped member -> NO edge
  this.foo();      // unscoped member -> NO edge
  return bc(2);    // plain call
}
EOF

# ── #2: cross-file named-call binding-scoping + collision ─────────────────────
echo 'export function collidedCall(x){return x+1;}' > "$FIX/callA.ts"
echo 'export function collidedCall(y){return y-1;}' > "$FIX/callB.ts"
cat > "$FIX/namedcall.ts" <<'EOF'
import { collidedCall } from "./callA";
function collidedCall_local(){ return 0; }
export function nc() {
  collidedCall();        // named import -> must resolve to callA.ts, NOT callB.ts
  collidedCall_local();  // plain local call -> resolves to local def
}
EOF

# ── malformed tsconfig fail-open (separate root) ─────────────────────────────
BAD="$FIX/_bad"
mkdir -p "$BAD"
echo '{ this is not valid json at all ]]' > "$BAD/tsconfig.json"
echo 'export function q(){return 1;}' > "$BAD/q.ts"
echo 'import {q} from "./q"; export function useq(){return q();}' > "$BAD/useq.ts"

( cd "$FIX" && git init -q && git add -A && git -c user.email=t@t -c user.name=t commit -qm init ) >/dev/null 2>&1

python3 "$CG" --path "$FIX" --full >/dev/null 2>&1
GRAPH="$(find "$FIX" -name code_graph.json 2>/dev/null | grep -v '/_bad/' | head -1)"
if [ -z "$GRAPH" ] || [ ! -f "$GRAPH" ]; then
  echo "FAIL: main graph json not produced"
  exit 1
fi

# malformed-tsconfig root must still build (fail-open, no crash)
( cd "$BAD" && git init -q && git add -A && git -c user.email=t@t -c user.name=t commit -qm b ) >/dev/null 2>&1
python3 "$CG" --path "$BAD" --full >/dev/null 2>&1
BADRC=$?
BADGRAPH="$(find "$BAD" -name code_graph.json 2>/dev/null | head -1)"

python3 - "$GRAPH" "$BADRC" "$BADGRAPH" <<'PY'
import json, sys
g = json.load(open(sys.argv[1]))
badrc = int(sys.argv[2])
badgraph = sys.argv[3]
nodes = g["nodes"]; edges = g.get("relation_edges", [])
byidx = {n["idx"]: n for n in nodes}

def efind(label, from_file=None, to_file=None, obj=None, resolved=None):
    for e in edges:
        if e["label"] != label: continue
        a = byidx.get(e["a"]); b = byidx.get(e["b"]) if e["b"] >= 0 else None
        if from_file and (a is None or from_file not in a.get("file","")): continue
        if to_file and (b is None or to_file not in b.get("file","")): continue
        if obj is not None and e.get("object") != obj: continue
        if resolved is True and e["b"] < 0: continue
        if resolved is False and e["b"] >= 0: continue
        return e
    return None

fails = []

# #1a alias
if not efind("imports", from_file="app.ts", to_file="src/util.ts", resolved=True):
    fails.append("#1a alias @/util -> src/util.ts missing")
if not efind("imports", from_file="app.ts", to_file="packages/lib/thing.ts", resolved=True):
    fails.append("#1a alias @lib/thing -> packages/lib/thing.ts missing")

# #1b barrel hop: useBarrel.ts must reach y.ts (flattened) AND barrel.ts
if not efind("imports", from_file="useBarrel.ts", to_file="y.ts", resolved=True):
    fails.append("#1b barrel flatten useBarrel.ts -> y.ts missing")
if not efind("imports", from_file="useBarrel.ts", to_file="barrel.ts", resolved=True):
    fails.append("#1b direct useBarrel.ts -> barrel.ts missing")
# cycle must not hang (we got here) and useCycle resolves to cycleA at least
if not efind("imports", from_file="useCycle.ts", to_file="cycleA.ts", resolved=True):
    fails.append("#1b cycle: useCycle.ts -> cycleA.ts missing")

# #2 named-call scoping: collidedCall() -> callA.ts (NOT callB.ts)
ca = efind("calls", from_file="namedcall.ts", to_file="callA.ts", obj="collidedCall", resolved=True)
cb = efind("calls", from_file="namedcall.ts", to_file="callB.ts", obj="collidedCall", resolved=True)
if ca is None:
    fails.append("#2 named call collidedCall() did NOT resolve to callA.ts")
if cb is not None:
    fails.append("#2 named call collidedCall() WRONGLY resolved to callB.ts (binding not scoped)")

# #3 member-scoping: a.collided() -> modA.ts (NOT modB.ts)
ma = efind("calls", from_file="member.ts", to_file="modA.ts", obj="collided", resolved=True)
mb = efind("calls", from_file="member.ts", to_file="modB.ts", obj="collided", resolved=True)
if ma is None:
    fails.append("#3 a.collided() did NOT resolve to modA.ts")
if mb is not None:
    fails.append("#3 a.collided() WRONGLY resolved to modB.ts (collision not scoped)")
# unscoped member drop: no calls edge whose object is 'map' or 'foo'
if any(e["label"] == "calls" and e.get("object") in ("map", "foo") for e in edges):
    fails.append("#3 unscoped member call (map/foo) was NOT dropped")

# malformed tsconfig fail-open: build succeeded + produced a graph
if badrc != 0 or not badgraph:
    fails.append("malformed-tsconfig root did NOT build (fail-open broken)")
else:
    bg = json.load(open(badgraph))
    if not any(e["label"] == "imports" and e["b"] >= 0 for e in bg.get("relation_edges", [])):
        # useq -> q should still resolve by relative/stem even without tsconfig
        fails.append("malformed-tsconfig: relative import useq->q did not resolve (regression)")

# #3 barrel provenance: the flattened useBarrel.ts -> y.ts edge must carry
# via=barrel.ts so code_query explain can annotate the hop.
barrel_edge = efind("imports", from_file="useBarrel.ts", to_file="y.ts", resolved=True)
if barrel_edge is None or barrel_edge.get("via") != "barrel.ts":
    fails.append("#3 flattened barrel edge missing via=barrel.ts provenance (got via=%r)"
                 % (barrel_edge.get("via") if barrel_edge else None))

if fails:
    print("RESULT: FAIL")
    for f in fails: print("  -", f)
    sys.exit(1)
print("RESULT: PASS")
print("  #1a alias + #1b barrel-hop/cycle + #2 named-call/collision + #3 member-scoping/unscoped-drop + barrel-provenance + malformed-fail-open all OK")
PY
exit $?
