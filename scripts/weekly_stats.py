#!/usr/bin/env python3
"""
weekly_stats.py — dinomem Weekly Stats Card (zero LLM, zero cost).

Counts memory files, graph nodes/edges, skill candidates, promoted skills,
and open project notes. Outputs a formatted stats card to stdout.

Usage:
  python3 scripts/weekly_stats.py [--plain] [--workspace PATH]

  --plain       ASCII-only output (for WhatsApp, SMS, plain terminals)
  --workspace   Path to agent workspace (default: auto-detect from script location)

Wire into cron (weekly, e.g. Sunday 09:00 local):
  0 9 * * 0 python3 <workspace>/scripts/weekly_stats.py >> /tmp/weekly_stats.log 2>&1
"""

import argparse
import json
import os
import re
from datetime import datetime
from pathlib import Path


def find_workspace(script_path: Path) -> Path:
    """Auto-detect workspace root from script location."""
    # scripts/ is typically inside the workspace or skill dir
    # Walk up to find a directory containing memory/ and MEMORY.md
    candidate = script_path.parent
    for _ in range(4):
        candidate = candidate.parent
        if (candidate / "memory").is_dir() and (candidate / "MEMORY.md").exists():
            return candidate
    # Fallback: use env var or cwd
    return Path(os.environ.get("DINOMEM_WORKSPACE", Path.cwd()))


def count_notes(memory_dir: Path):
    notes = list(memory_dir.glob("_note_*.md"))
    pins = list(memory_dir.glob("_pin_*.md"))
    done_projects = 0
    open_projects = 0
    for f in notes:
        try:
            txt = f.read_text(encoding="utf-8")
            if "type: project" in txt:
                if "status: done" in txt:
                    done_projects += 1
                elif "status: in_progress" in txt:
                    open_projects += 1
        except Exception:
            pass
    return len(notes), len(pins), done_projects, open_projects


def count_graph(kb_dir: Path):
    graph_path = kb_dir / "memory_neuron/l2_graph/memory_graph.json"
    if not graph_path.exists():
        return 0, 0
    try:
        data = json.loads(graph_path.read_text(encoding="utf-8"))
        return data.get("node_count", 0), data.get("edge_count", 0)
    except Exception:
        return 0, 0


def count_skills(kb_dir: Path, skills_dir: Path):
    candidates_dir = kb_dir / "memory_neuron/skill_candidates"
    candidates = len(list(candidates_dir.glob("*/meta.json"))) if candidates_dir.exists() else 0
    promoted = len(list(skills_dir.glob("*/SKILL.md"))) if skills_dir.exists() else 0
    return candidates, promoted


def count_memory_files(memory_dir: Path):
    return len(list(memory_dir.glob("*.md")))


def render(ws: Path, plain: bool) -> str:
    memory_dir = ws / "memory"
    kb_dir = ws / "kb"
    skills_dir = ws / "skills"

    total_files = count_memory_files(memory_dir)
    notes, pins, done_projects, open_projects = count_notes(memory_dir)
    nodes, edges = count_graph(kb_dir)
    candidates, promoted = count_skills(kb_dir, skills_dir)

    date_str = datetime.now().strftime("%Y-%m-%d")

    if plain:
        sep = "-" * 26
        brain = "dinomem"
        check = "*"
    else:
        sep = "━" * 26
        brain = "dinomem"
        check = "✓"

    lines = [
        sep,
        f"  {brain} Weekly Stats",
        sep,
        f"  {date_str}",
        "",
        "  Memory",
        f"    Files:         {total_files}",
        f"    Notes:         {notes}  ({open_projects} open, {done_projects} done)",
        f"    Pins:          {pins}",
        "",
        "  Graph",
        f"    Nodes:         {nodes}",
        f"    Edges:         {edges}",
        "",
        "  Skills",
        f"    Candidates:    {candidates}",
        f"    Promoted:      {promoted}",
        sep,
    ]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="dinomem weekly stats card")
    ap.add_argument("--plain", action="store_true", help="ASCII-only output")
    ap.add_argument("--workspace", metavar="PATH", help="Workspace root path")
    args = ap.parse_args()

    if args.workspace:
        ws = Path(args.workspace).expanduser().resolve()
    else:
        ws = find_workspace(Path(__file__).resolve())

    print(render(ws, plain=args.plain))


if __name__ == "__main__":
    main()
