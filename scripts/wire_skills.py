#!/usr/bin/env python3
"""
Wire dinomem workspace skills into the target agent's skill allowlist.

This script is run by install.sh after skills are copied into the workspace.
It makes the installed skills actually usable by the target agent by adding the
shipped skill IDs to the agent's agents.list[].skills allowlist.

If the target agent has no explicit skills allowlist, it inherits defaults; in
that case we fall back to mutating agents.defaults.skills.

Multi-agent safe: only the agent matching --agent-id is touched. Other agents'
allowlists are left unchanged.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path


def _strip_json5_comments(raw: str) -> str:
    """Best-effort strip of // and /* */ comments so json.loads works."""
    raw = re.sub(r'(^|[^:"])//.*', r'\1', raw)
    raw = re.sub(r'/\*.*?\*/', '', raw, flags=re.DOTALL)
    return raw


def load_json_robust(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return json.loads(_strip_json5_comments(text))


def main():
    parser = argparse.ArgumentParser(description="Wire workspace skills into agent allowlist")
    parser.add_argument("--workspace", required=True, help="Agent workspace directory")
    parser.add_argument("--agent-id", required=True, help="OpenClaw agent ID")
    parser.add_argument("--skills-dir", required=True, help="Repo skills/ directory")
    parser.add_argument("--dry-run", action="store_true", help="Print patch, do not apply")
    args = parser.parse_args()

    ws = Path(args.workspace).resolve()
    skills_dir = Path(args.skills_dir).resolve()
    cfg_path = Path(os.environ.get("OPENCLAW_CONFIG", Path.home() / ".openclaw/openclaw.json")).expanduser()

    if not cfg_path.exists():
        print(f"WARNING: openclaw.json not found at {cfg_path}; skipping skill wiring", file=sys.stderr)
        sys.exit(0)

    shipped = sorted(
        d.name for d in skills_dir.iterdir()
        if d.is_dir() and (d / "SKILL.md").exists()
    )
    if not shipped:
        print("no skills to wire")
        return

    cfg = load_json_robust(cfg_path)

    # Update agent-specific allowlist, or fallback to defaults.
    agents_list = cfg.get("agents", {}).get("list", [])
    target = next((a for a in agents_list if a.get("id") == args.agent_id), None)

    if target is not None:
        current = list(target.get("skills", []))
        new = sorted(set(current) | set(shipped))
        if new != current:
            target["skills"] = new
            print(f"agent '{args.agent_id}' allowlist: {len(current)} -> {len(new)} skills")
        else:
            print(f"agent '{args.agent_id}' allowlist already up-to-date")
    else:
        defaults = cfg.setdefault("agents", {}).setdefault("defaults", {})
        current = list(defaults.get("skills", []))
        new = sorted(set(current) | set(shipped))
        if new != current:
            defaults["skills"] = new
            print(f"agents.defaults allowlist: {len(current)} -> {len(new)} skills (agent '{args.agent_id}' not found)")
        else:
            print(f"agents.defaults allowlist already up-to-date")

    if args.dry_run:
        print(json.dumps({
            "shipped": shipped,
            "target_agent": args.agent_id,
            "would_update": "agent" if target is not None else "defaults",
        }, indent=2))
        return

    patch = cfg
    fd, patch_file = tempfile.mkstemp(suffix=".json", prefix="dinomem_wire_skills_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(patch, f, indent=2)

        r = subprocess.run(
            ["openclaw", "config", "patch", "--file", patch_file],
            capture_output=True, text=True, timeout=120, check=False,
        )
        print(r.stdout)
        if r.stderr:
            print(r.stderr, file=sys.stderr)
        sys.exit(r.returncode)
    finally:
        try:
            os.unlink(patch_file)
        except OSError:
            pass


if __name__ == "__main__":
    main()
