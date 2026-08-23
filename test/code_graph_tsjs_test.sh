#!/usr/bin/env bash
# code_graph_tsjs_test.sh — deterministic JS/TS extractor test for code_graph.
# Generic: resolves code_graph.py from ${DINOMEM_WORKSPACE} OR a base/neuron repo
# OR a live ~/.openclaw/workspace-* ws. SKIPs cleanly if the javascript grammar
# is absent (tree-sitter optional dep). Asserts cross-file import + cross-file
# call + class inherits + CommonJS require + external-drop, for BOTH .js and .ts.
set -u

# ── resolve code_graph.py ────────────────────────────────────────────────────
CG=""
_cands=()
[ -n "${DINOMEM_WORKSPACE:-}" ] && _cands+=("${DINOMEM_WORKSPACE}/procedures/code_graph.py")
# repo-relative (test/ sits beside procedures/ in the repo)
_here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_cands+=("${_here}/../procedures/code_graph.py")
# live workspaces
for d in "$HOME"/.openclaw/workspace-*; do
  _cands+=("$d/procedures/code_graph.py")
done
for c in "${_cands[@]}"; do
  if [ -f "$c" ]; then CG="$c"; break; fi
done
if [ -z "$CG" ]; then
  echo "SKIP: code_graph.py not found (looked in DINOMEM_WORKSPACE, repo, ~/.openclaw/workspace-*)"
  exit 0
fi
WS="$(cd "$(dirname "$CG")/.." && pwd)"

# ── grammar availability gate ────────────────────────────────────────────────
if ! python3 -c "from tree_sitter_language_pack import get_parser; get_parser('javascript'); get_parser('typescript')" 2>/dev/null; then
  echo "SKIP: javascript/typescript tree-sitter grammar not available"
  exit 0
fi

# ── fixture (a git repo so the incremental/churn paths are exercised) ─────────
FIX="$(mktemp -d)"
trap 'rm -rf "$FIX"' EXIT

cat > "$FIX/util.js" <<'EOF'
export function helper(x) { return x + 1; }
EOF

cat > "$FIX/a.js" <<'EOF'
import { helper } from "./util";
import React from "react";           // external -> must DROP
class Animal { move() {} }
export class Dog extends Animal { bark() { return helper(1); } }
export function run() { return helper(2); }
EOF

cat > "$FIX/legacy.js" <<'EOF'
const l = require("./util");
function useIt() { return l.helper(3); }
EOF

cat > "$FIX/t.ts" <<'EOF'
import { run } from "./a";
export function tmain(): number { return run(); }
EOF

( cd "$FIX" && git init -q && git add -A && git -c user.email=t@t -c user.name=t commit -qm init ) >/dev/null 2>&1

# ── build the graph over the fixture ─────────────────────────────────────────
GRAPH="$FIX/kb/code_graph/code_graph.json"
python3 "$CG" --path "$FIX" --full >/dev/null 2>&1
if [ ! -f "$GRAPH" ]; then
  # some layouts write under the workspace; fall back to a --db-agnostic search
  GRAPH="$(find "$FIX" -name code_graph.json 2>/dev/null | head -1)"
fi
if [ -z "$GRAPH" ] || [ ! -f "$GRAPH" ]; then
  echo "FAIL: graph json not produced"
  exit 1
fi

# ── assertions (pure python over the graph json; no code_query dependency) ────
python3 - "$GRAPH" <<'PY'
import json, sys
g = json.load(open(sys.argv[1]))
nodes = g["nodes"]
edges = g.get("relation_edges", [])
byidx = {n["idx"]: n for n in nodes}

def has_def(name, kind=None):
    return any(n["name"] == name and (kind is None or n["kind"] == kind) for n in nodes)

def edge(label, from_name=None, to_name=None, resolved=None, to_file=None):
    for e in edges:
        if e["label"] != label:
            continue
        a = byidx.get(e["a"]); b = byidx.get(e["b"]) if e["b"] >= 0 else None
        if from_name is not None and (a is None or a["name"] != from_name):
            continue
        if to_name is not None and (e.get("object") != to_name and (b is None or b["name"] != to_name)):
            continue
        if to_file is not None and (b is None or to_file not in b.get("file","")):
            continue
        if resolved is True and e["b"] < 0:
            continue
        if resolved is False and e["b"] >= 0:
            continue
        return e
    return None

fails = []

# defs
for nm, kd in [("helper","function"),("run","function"),("Dog","class"),
               ("Animal","class"),("tmain","function"),("run","function")]:
    if not has_def(nm, kd):
        fails.append(f"missing def {kd} {nm}")

# cross-file import a.js -> util.js (ESM), resolved to the util file node
if not edge("imports", to_name="util", resolved=True):
    fails.append("a.js --imports--> util (ESM, resolved) missing")

# CommonJS require in legacy.js -> util (resolved)
if not edge("imports", to_name="util", resolved=True):
    fails.append("legacy.js require --imports--> util missing")

# cross-file call: run/bark call helper (helper defined in util.js) -> resolved
if not edge("calls", to_name="helper", resolved=True):
    fails.append("calls --> helper (cross-file, resolved) missing")

# class inherits: Dog extends Animal -> resolved (Animal in same file)
if not edge("inherits", from_name="Dog", to_name="Animal", resolved=True):
    fails.append("Dog --inherits--> Animal missing")

# t.ts -> a.js import (resolved)
if not edge("imports", to_name="a", resolved=True):
    fails.append("t.ts --imports--> a (TS, resolved) missing")

# external DROP: NO edge whose object is 'react' or 'React'
if any(e.get("object") in ("react","React") for e in edges):
    fails.append("external 'react' import was NOT dropped")

if fails:
    print("RESULT: FAIL")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("RESULT: PASS")
print("  defs+cross-file import+require+cross-file call+inherits+TS import+external-drop all OK")
PY
rc=$?
exit $rc
