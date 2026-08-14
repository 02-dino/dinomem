#!/usr/bin/env python3
"""
drive_neuron.py — force the dinomem BASE+NEURON memory pipeline to convergence in
an ISOLATED lab workspace, so the harness measures a fully-built neuron memory
(not a half-built, cron-async one).

WHY A SEPARATE DRIVER (not drive_base.py):
  Neuron is an UPGRADE LAYER over base. It adds pipeline stages base does not have,
  all of which are cron-materialized in production (so absent in a fresh lab unless
  forced). If the answer loop queried neuron memory before these ran, hybrid_recall
  would read empty vector/graph DBs and score unfairly low. This driver runs the
  full base chain THEN the neuron-only L2/L3/L4 stages, synchronously, to
  steady-state, then asserts BOTH base memory items>0 AND graph nodes>0 before
  anyone queries.

NEURON PIPELINE ORDER (verified from github/dinomem-neuron/scripts/install.sh cron
block — do NOT reorder):
  BASE front door (session_reset -> extract_memory -> user legs), and when neuron
  is installed the front door ALSO fires session_ingest.py (vector-indexes the
  archived sessions). Then base cleanup + review LOOP. Then the neuron cron stages,
  which we force in their prod order:
    L2  memory_graph.py            (daily 3:00 UTC, no-LLM relational linking)
    L3  memory_synthesis.py        (daily 3:30, reasoning=True cross-item synthesis)
        contradiction_check.py     (same slot)
        confidence_engine.py       (same slot, calibration)
    L4  memory_promote.py          (weekly Sun 4:00, no-LLM promotion to _permanent)
        generate_topic_index.py    (same slot, no-reasoning topic index)
  First graph/synthesis runs can take several MINUTES (installer note) -> use a
  larger per-stage timeout than base.

ISOLATION INVARIANT (safety, non-negotiable):
  In the REAL lab layout (setup_lab.py --layout real), the WS is <root>/workspace-<agent>/
  and the sessions dir is <root>/agents/<agent>/sessions/ — BOTH inside the sandbox
  root. Every stage runs cwd=WS with DINOMEM_WORKSPACE/OPENCLAW_WORKSPACE pointed at
  the WS. After the run we re-check the live source mtime and FAIL LOUD if the live
  user install was touched. The neuron overlay MUST have already been applied to the
  WS (run.py does this via the neuron installer with --no-cron --no-auto-base) — this
  driver only FORCES the pipeline; it does not install.

USAGE:
  python3 drive_neuron.py --ws <WORKSPACE_DIR> [--json] [--timeout 1200] \\
                          [--review-max-loops 12] [--live-source <DIR>] \\
                          [--live-source-mtime <FLOAT>] [--sandbox-root <DIR>]

  --ws           : the neuron-overlaid workspace (<root>/workspace-<agent> from
                   setup_lab.py --layout real, after the neuron installer ran on it).
  --sandbox-root : the lab root that carries the .dinomem_lab sentinel (default:
                   parent of --ws). Used for the sentinel guard + isolation.

  Exit 0 only if: front door ok, base items>0, graph nodes>0, and the live
  workspace mtime is unchanged. Any failure -> nonzero exit, fail-loud.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

def _fail(msg: str, code: int = 1):
    print(f"[drive_neuron] FAIL: {msg}", file=sys.stderr)
    sys.exit(code)

def _log(msg: str):
    print(f"[drive_neuron] {msg}", file=sys.stderr)

def _run_stage(name: str, script: Path, ws: Path, env: dict, timeout: int,
               reasoning: bool = False) -> dict:
    """Run one pipeline stage script from the WS procedures dir, cwd=ws.

    A missing optional script returns ran=False (skipped), never fails. Some neuron
    L3 stages want reasoning enabled; we set DINOMEM_REASONING=1 for those (the
    installer's cron does the equivalent via a REASON_ENV prefix).
    """
    if not script.exists():
        _log(f"stage '{name}': script absent ({script.name}) — skipped")
        return {"stage": name, "ran": False, "ok": True, "rc": None,
                "timed_out": False, "skipped": True}
    stage_env = dict(env)
    if reasoning:
        stage_env["DINOMEM_REASONING"] = "1"
    _log(f"stage '{name}': running {script.name} (cwd={ws} reasoning={reasoning})")
    t0 = time.time()
    try:
        r = subprocess.run(
            [sys.executable, str(script)],
            cwd=str(ws), env=stage_env, timeout=timeout,
            capture_output=True, text=True,
        )
        timed_out = False
        rc = r.returncode
        out, err = r.stdout, r.stderr
    except subprocess.TimeoutExpired as e:
        timed_out = True
        rc = None
        out = (e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        err = (e.stderr or b"").decode("utf-8", "replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
    dt = round(time.time() - t0, 1)
    ok = (rc == 0) and not timed_out
    _log(f"stage '{name}': rc={rc} timed_out={timed_out} {dt}s")
    return {
        "stage": name, "ran": True, "ok": ok, "rc": rc,
        "timed_out": timed_out, "seconds": dt,
        "stdout_tail": (out or "")[-800:],
        "stderr_tail": (err or "")[-800:],
    }

def _count_memory_items(ws: Path) -> dict:
    """Base memory materialization: per-item files + MEMORY.md body in <ws>/memory."""
    mem = ws / "memory"
    if not mem.exists():
        return {"item_files": 0, "memory_md_bytes": 0}
    item_files = [
        p for p in mem.glob("*.md")
        if p.name != "MEMORY.md" and not p.name.startswith("_")
    ]
    memory_md = mem / "MEMORY.md"
    return {
        "item_files": len(item_files),
        "memory_md_bytes": memory_md.stat().st_size if memory_md.exists() else 0,
    }

def _count_graph_nodes(ws: Path) -> dict:
    """Neuron L2 materialization: node count in the memory graph. Neuron writes
    kb/memory_neuron/l2_graph/memory_graph.json (see graph_search.py TOOLS.md db
    path). Fail-open: missing/unparseable -> 0 nodes (assertion will catch it).
    """
    candidates = [
        ws / "kb" / "memory_neuron" / "l2_graph" / "memory_graph.json",
        ws / "kb" / "memory_neuron" / "memory_graph.json",
    ]
    for gpath in candidates:
        if not gpath.exists():
            continue
        try:
            data = json.loads(gpath.read_text(encoding="utf-8"))
        except Exception:
            continue
        # graph_store writes {"nodes": {...}|[...], "edges": [...]} — count either shape.
        nodes = data.get("nodes")
        if isinstance(nodes, dict):
            n = len(nodes)
        elif isinstance(nodes, list):
            n = len(nodes)
        else:
            n = 0
        edges = data.get("edges")
        e = len(edges) if isinstance(edges, (list, dict)) else 0
        return {"graph_path": str(gpath), "nodes": n, "edges": e}
    return {"graph_path": None, "nodes": 0, "edges": 0}

def _live_source_mtime(source: Path) -> float:
    latest = 0.0
    for sub in ("procedures", "tools"):
        d = source / sub
        if not d.exists():
            continue
        for p in d.rglob("*"):
            try:
                latest = max(latest, p.stat().st_mtime)
            except OSError:
                pass
    return latest

# Neuron cron stages in prod order. (script_name, reasoning_flag)
NEURON_STAGES = [
    ("memory_graph.py", False),        # L2
    ("memory_synthesis.py", True),     # L3
    ("contradiction_check.py", True),  # L3
    ("confidence_engine.py", False),   # L3 calibration
    ("memory_promote.py", False),      # L4
    ("generate_topic_index.py", False) # L4 topic index
]

def drive(ws: Path, sandbox_root: Path, timeout: int, review_max_loops: int,
          live_source: Path | None, live_source_mtime: float | None,
          skip_stages: set[str] | None = None) -> dict:
    # skip_stages = neuron L2/L3/L4 script names to ABLATE (Phase 5b). When the graph
    # stage itself is ablated, the graph-node assertion is relaxed so the ablated run
    # can still 'converge' (the whole point is measuring life WITHOUT that mechanism).
    skip_stages = skip_stages or set()
    ws = ws.resolve()
    if not ws.exists():
        _fail(f"workspace dir does not exist: {ws}")
    if not (sandbox_root / ".dinomem_lab").exists():
        _fail(f"refusing to drive: sandbox root {sandbox_root} lacks the "
              f".dinomem_lab sentinel (not a harness lab built by setup_lab.py "
              f"--layout real)")
    procs = ws / "procedures"
    if not procs.exists():
        _fail(f"workspace has no procedures/ dir: {procs}")

    # Sandbox env: point every workspace-var at the WS.
    env = dict(os.environ)
    env["DINOMEM_WORKSPACE"] = str(ws)
    env["OPENCLAW_WORKSPACE"] = str(ws)
    env["PYTHONUNBUFFERED"] = "1"
    # LongMemEval packs a whole multi-session haystack into ONE archive, so the
    # extraction LLM must emit a much larger JSON than a normal incremental
    # session. extract_memory's default LLM_MAX_TOKENS=3000 truncates that JSON
    # mid-string -> parse fail -> 0 items -> false no_base_memory bail. Give the
    # pipeline a haystack-sized budget (overridable from the outer env).
    env.setdefault("LLM_MAX_TOKENS", os.environ.get("LLM_MAX_TOKENS", "16000"))

    stages: list[dict] = []

    # ---- BASE FRONT DOOR: session_reset -> extract_memory -> user legs (+session_ingest if neuron) ----
    orchestrator = procs / "auto_session_reset.py"
    if orchestrator.exists():
        stages.append(_run_stage("front_door(auto_session_reset)", orchestrator,
                                 ws, env, timeout))
    else:
        stages.append(_run_stage("session_reset", procs / "session_reset.py",
                                 ws, env, timeout))
        stages.append(_run_stage("extract_memory", procs / "extract_memory.py",
                                 ws, env, timeout))
        # session_ingest is a neuron addition; run it explicitly if the front door
        # fallback path was taken (the real orchestrator would have chained it).
        stages.append(_run_stage("session_ingest", procs / "session_ingest.py",
                                 ws, env, timeout))

    critical = [s for s in stages if s["ran"]]
    if not critical or not any(s["ok"] for s in critical):
        return {"ok": False, "reason": "front_door_failed", "stages": stages,
                "memory": _count_memory_items(ws), "graph": _count_graph_nodes(ws)}

    # ---- BASE cleanup + review LOOP (same as base arm) ----
    stages.append(_run_stage("memory_cleanup", procs / "memory_cleanup.py",
                             ws, env, timeout))
    review_script = procs / "memory_review.py"
    if review_script.exists():
        prev_sig = None
        for i in range(review_max_loops):
            s = _run_stage(f"memory_review[{i}]", review_script, ws, env, timeout)
            stages.append(s)
            sig = _count_memory_items(ws)
            cur_sig = (sig["item_files"], sig["memory_md_bytes"])
            if not s["ok"]:
                break
            if cur_sig == prev_sig:
                _log(f"review converged after {i+1} iteration(s)")
                break
            prev_sig = cur_sig

    # ---- NEURON L2/L3/L4 stages, forced in prod cron order (minus ablated) ----
    for script_name, reasoning in NEURON_STAGES:
        if script_name in skip_stages:
            stages.append({"stage": f"neuron:{script_name}", "ran": False, "ok": True,
                           "rc": None, "ablated": True})
            _log(f"stage 'neuron:{script_name}': ABLATED (5b skip)")
            continue
        stages.append(_run_stage(f"neuron:{script_name}", procs / script_name,
                                 ws, env, timeout, reasoning=reasoning))

    mem = _count_memory_items(ws)
    graph = _count_graph_nodes(ws)

    # ---- CONVERGENCE ASSERTIONS ----
    # Base memory must materialize (same as base arm) AND the neuron graph must have
    # nodes (else L2/L3 features are inert and hybrid_recall's graph leg is empty).
    base_ok = mem["item_files"] > 0 or mem["memory_md_bytes"] > 0
    graph_ablated = "memory_graph.py" in skip_stages
    graph_ok = graph_ablated or graph["nodes"] > 0

    # ---- ISOLATION TRIPWIRE ----
    iso = {"checked": False}
    if live_source is not None:
        cur = _live_source_mtime(live_source.resolve())
        iso = {
            "checked": True,
            "live_source": str(live_source),
            "expected_mtime": live_source_mtime,
            "actual_mtime": cur,
            "untouched": (live_source_mtime is None) or (abs(cur - live_source_mtime) < 1e-6),
        }

    ok = base_ok and graph_ok and (not iso["checked"] or iso["untouched"])
    reason = None
    if not base_ok:
        reason = "no_base_memory (items==0 — front door/extract no-oped)"
    elif not graph_ok:
        reason = ("no_graph_nodes (memory_graph produced 0 nodes — neuron L2 "
                  "did not materialize; hybrid_recall graph leg would be empty)")
    # note: when graph_ablated, graph_ok is forced True (ablation is intentional)
    elif iso["checked"] and not iso["untouched"]:
        reason = "ISOLATION VIOLATION: live source mtime changed during run"

    return {"ok": ok, "reason": reason, "stages": stages, "memory": mem,
            "graph": graph, "isolation": iso}

def main():
    ap = argparse.ArgumentParser(description="Drive dinomem BASE+NEURON pipeline to convergence in a lab WS.")
    ap.add_argument("--ws", required=True, help="neuron-overlaid workspace dir (setup_lab --layout real, post-overlay)")
    ap.add_argument("--sandbox-root", help="lab root carrying .dinomem_lab sentinel (default: parent of --ws)")
    ap.add_argument("--timeout", type=int, default=1200, help="per-stage timeout seconds (graph/synthesis can be minutes)")
    ap.add_argument("--review-max-loops", type=int, default=12, help="max memory_review iterations")
    ap.add_argument("--live-source", help="live install dir to assert untouched (isolation tripwire)")
    ap.add_argument("--live-source-mtime", type=float, help="expected live source mtime from setup_lab.py")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    ap.add_argument("--skip-stage", action="append", default=[], metavar="SCRIPT",
                    help="ablate a neuron stage by script name (Phase 5b); repeatable. "
                         "e.g. --skip-stage memory_graph.py --skip-stage memory_promote.py")
    args = ap.parse_args()

    ws = Path(args.ws)
    sandbox_root = Path(args.sandbox_root) if args.sandbox_root else ws.resolve().parent
    live_source = Path(args.live_source) if args.live_source else None
    result = drive(ws, sandbox_root, args.timeout, args.review_max_loops,
                   live_source, args.live_source_mtime, skip_stages=set(args.skip_stage))

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"driver ok={result['ok']} reason={result.get('reason')}")
        print(f"  base memory: {result['memory']['item_files']} item file(s), "
              f"MEMORY.md {result['memory']['memory_md_bytes']} bytes")
        print(f"  graph: {result['graph']['nodes']} node(s), "
              f"{result['graph']['edges']} edge(s)")
        iso = result.get("isolation", {})
        if iso.get("checked"):
            print(f"  isolation: live source untouched={iso['untouched']}")
        for s in result["stages"]:
            if s.get("ran"):
                print(f"  stage {s['stage']}: ok={s['ok']} rc={s.get('rc')}")

    sys.exit(0 if result["ok"] else 2)

if __name__ == "__main__":
    main()
