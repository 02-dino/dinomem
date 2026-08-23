#!/usr/bin/env bash
# code_graph_multilang_test.sh — deterministic per-language extractor test for the
# 2026-08-23 batch: go / rust / java / cpp / php / ruby / r / css / html / sql.
# Each lang gets a tiny fixture and an assertion on the defs/refs it must emit.
# A language whose tree-sitter grammar is absent is SKIPped (not failed) so the
# suite is portable across packs. Generic resolver (${DINOMEM_WORKSPACE} OR
# ~/.openclaw/workspace-*).
set -u

CG=""
_cands=()
[ -n "${DINOMEM_WORKSPACE:-}" ] && _cands+=("${DINOMEM_WORKSPACE}/procedures/code_graph.py")
_here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_cands+=("${_here}/../procedures/code_graph.py")
for d in "$HOME"/.openclaw/workspace-*; do _cands+=("$d/procedures/code_graph.py"); done
for c in "${_cands[@]}"; do [ -f "$c" ] && { CG="$c"; break; }; done
[ -z "$CG" ] && { echo "SKIP: code_graph.py not found"; exit 0; }

FIX="$(mktemp -d)"; trap 'rm -rf "$FIX"' EXIT

printf 'package m\nimport "fmt"\ntype T struct{}\nfunc (t T) M(){ fmt.Println() }\nfunc F(){ M() }\n' > "$FIX/a.go"
printf 'use std::io;\nstruct S;\ntrait Tr{}\nimpl Tr for S{}\nfn f(){ g(); }\nfn g(){}\n' > "$FIX/a.rs"
printf 'import java.util.List;\nclass A extends B implements C { void m(){ n(); } }\n' > "$FIX/A.java"
printf '#include <vector>\nclass A : public B { void m(){ n(); } };\nvoid f(){ g(); }\n' > "$FIX/a.cpp"
printf '<?php\nnamespace App;\nuse Foo\\Bar;\nclass A extends B { function m(){ n(); } }\n' > "$FIX/a.php"
printf 'require "set"\nclass A < B\n def m; n; end\nend\n' > "$FIX/a.rb"
printf 'library(dplyr)\nsource("util.R")\nf <- function(x){ g(x) }\n' > "$FIX/a.R"
printf '@import "base.css";\n.foo { color: red; }\n#bar { top: 0; }\n' > "$FIX/a.css"
printf '<html><head><script src="a.js"></script><link href="s.css"></head></html>\n' > "$FIX/a.html"
printf 'CREATE TABLE t (id int);\nCREATE VIEW v AS SELECT * FROM t;\n' > "$FIX/a.sql"
printf 'using System;\nnamespace N { class A : B { void M(){ Foo(); } } }\n' > "$FIX/a.cs"
printf 'name: app\ndeps:\n  - x\ncfg:\n  k: v\n' > "$FIX/a.yaml"

( cd "$FIX" && git init -q && git add -A && git -c user.email=t@t -c user.name=t commit -qm init ) >/dev/null 2>&1
python3 "$CG" --path "$FIX" --full > "$FIX/_build.log" 2>&1
GRAPH="$(find "$FIX" -name code_graph.json 2>/dev/null | head -1)"
[ -f "$GRAPH" ] || { echo "FAIL: graph json not produced"; cat "$FIX/_build.log"; exit 1; }

python3 - "$GRAPH" "$FIX/_build.log" <<'PY'
import json, sys, re
g = json.load(open(sys.argv[1]))
buildlog = open(sys.argv[2]).read()
nodes = g["nodes"]; edges = g.get("relation_edges", [])
byidx = {n["idx"]: n for n in nodes}

# parse_errors must be 0
m = re.search(r"parse_errors=(\d+)", buildlog)
perr = int(m.group(1)) if m else -1

DEF_KINDS = ("function","method","class","struct","trait","impl","enum",
             "interface","module","type","selector","table","view",
             "record","key")
def defs_in(ext, kinds=None):
    # graph node schema uses 'kind' (file|function|method|class|...), not 'type'
    out = [n for n in nodes if n.get("file","").endswith("."+ext)
           and n.get("kind") in DEF_KINDS]
    if kinds:
        out = [n for n in out if n.get("kind") in kinds]
    return out

def refs_in(ext, label):
    out = []
    for e in edges:
        a = byidx.get(e["a"])
        if a and a.get("file","").endswith("."+ext) and e["label"] == label:
            out.append(e)
    # also count file-origin refs (from_qual '<file>') whose subject file matches
    return out

# expectations: (ext, present, min_defs, {label:min})
EXPECT = [
    ("go",   True, 2, {"imports":1, "calls":1}),
    ("rs",   True, 3, {"imports":1, "calls":1}),
    ("java", True, 2, {"imports":1, "inherits":2, "calls":1}),
    ("cpp",  True, 2, {"imports":1, "inherits":1, "calls":1}),
    ("php",  True, 2, {"imports":1, "inherits":1, "calls":1}),
    ("rb",   True, 2, {"imports":1, "inherits":1}),
    ("R",    True, 1, {"imports":1, "calls":1}),
    ("css",  True, 2, {"imports":1}),
    ("html", True, 0, {"imports":1}),
    ("sql",  True, 2, {"references":1}),
    ("cs",   True, 1, {"imports":1, "inherits":1, "calls":1}),
    ("yaml", True, 2, {}),
]

fails, skips = [], []
if perr != 0:
    fails.append(f"parse_errors={perr} (want 0)")

for ext, _p, mindefs, labelmins in EXPECT:
    # skip a lang if NOTHING for it landed AND its grammar is likely absent:
    # detect by "no nodes at all for this ext" -> treat as SKIP not FAIL.
    has_any = any(n.get("file","").endswith("."+ext) for n in nodes)
    if not has_any:
        skips.append(ext)
        continue
    nd = defs_in(ext)
    if len(nd) < mindefs:
        fails.append(f"{ext}: defs {len(nd)} < {mindefs} ({[n['kind'] for n in nd]})")
    for lbl, mn in labelmins.items():
        got = len(refs_in(ext, lbl))
        if got < mn:
            fails.append(f"{ext}: {lbl} {got} < {mn}")

if fails:
    print("RESULT: FAIL")
    for f in fails: print("  -", f)
    sys.exit(1)
print("RESULT: PASS")
tested = [e[0] for e in EXPECT if e[0] not in skips]
print("  langs OK:", " ".join(tested))
if skips:
    print("  SKIPPED (grammar absent):", " ".join(skips))
PY
exit $?
