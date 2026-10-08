#!/usr/bin/env python3
"""
Wire dinomem workspace skills into the target agent's skill allowlist.

This script is run by install.sh after skills are copied into the workspace.
It makes the installed skills actually usable by the target agent by adding the
shipped skill IDs to the agent's agents.list[].skills allowlist.

Multi-agent safe: only the best matching agent is touched. Other agents'
allowlists are left unchanged.

It REFUSES to create agents.defaults.skills out of nothing. An absent defaults
allowlist means every inheriting agent is unrestricted, so writing one would
revoke every skill this package does not ship -- for every one of those agents.
Granting must never revoke. Merging into a defaults allowlist that ALREADY
exists stays allowed, because that is additive.

Writes via openclaw config patch --replace-path so it never touches read-only
meta fields like lastTouchedVersion.
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


def find_target_agent(agents_list, agent_id, ws):
    """Exact id match > workspace path match > substring id match."""
    target = next((a for a in agents_list if a.get("id") == agent_id), None)
    if target is not None:
        return target
    # Path == Path. (Was str == Path: always False, so this whole tier was dead
    # and every real-but-unlisted agent fell through to the defaults branch.)
    target = next(
        (
            a for a in agents_list
            if a.get("workspace")
            and Path(a["workspace"]).expanduser().resolve() == ws
        ),
        None,
    )
    if target is not None:
        return target
    needle = agent_id.lower()
    candidates = [
        a for a in agents_list
        if needle in a.get("id", "").lower() or a.get("id", "").lower() in needle
    ]
    if candidates:
        return min(candidates, key=lambda a: len(a.get("id", "")))
    return None


def main():
    parser = argparse.ArgumentParser(description="Wire workspace skills into agent allowlist")
    parser.add_argument("--workspace", required=True, help="Agent workspace directory")
    parser.add_argument("--agent-id", required=True, help="OpenClaw agent ID")
    parser.add_argument("--skills-dir", required=True, help="Repo skills/ directory")
    parser.add_argument("--dry-run", action="store_true", help="Print patch, do not apply")
    parser.add_argument(
        "--allow-defaults-create",
        action="store_true",
        help="Permit CREATING agents.defaults.skills when no agent matched. Off by "
             "default: it revokes every unshipped skill for all inheriting agents.",
    )
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
    agents_list = cfg.get("agents", {}).get("list", [])
    target = find_target_agent(agents_list, args.agent_id, ws)

    if target is not None:
        target_id = target.get("id")
        current = list(target.get("skills", []))
        new = sorted(set(current) | set(shipped))
        if new == current:
            print(f"agent '{target_id}' allowlist already up-to-date ({len(current)} skills)")
            return
        target["skills"] = new
        print(f"agent '{target_id}' allowlist: {len(current)} -> {len(new)} skills")

        patch = {"agents": {"list": agents_list}}
        replace_path = "agents.list"
    else:
        defaults = cfg.get("agents", {}).get("defaults", {})
        current = list(defaults.get("skills", []))
        # No agent matched. Creating a defaults allowlist where none existed turns
        # "all skills visible" into "only these N" for every inheriting agent -- a
        # mass silent revoke, caused by an install whose job was to GRANT. Refuse.
        if not current and not args.allow_defaults_create:
            print(
                f"ERROR: agent '{args.agent_id}' not found in agents.list, and "
                f"agents.defaults.skills is unset (= all skills allowed).\n"
                f"  Refusing to create a defaults allowlist: it would revoke every "
                f"skill not shipped here, for EVERY agent inheriting defaults.\n"
                f"  Fix one of:\n"
                f"    - register the agent in agents.list (matched by id or workspace), or\n"
                f"    - pass the real --agent-id, or\n"
                f"    - re-run with --allow-defaults-create if a global allowlist is "
                f"genuinely intended.\n"
                f"  Shipped skills left unwired: {', '.join(shipped)}",
                file=sys.stderr,
            )
            sys.exit(2)
        new = sorted(set(current) | set(shipped))
        if new == current:
            print(f"agents.defaults allowlist already up-to-date ({len(current)} skills)")
            return
        defaults["skills"] = new
        print(f"agents.defaults allowlist: {len(current)} -> {len(new)} skills (agent '{args.agent_id}' not found)")

        patch = {"agents": {"defaults": {"skills": new}}}
        replace_path = "agents.defaults.skills"

    if args.dry_run:
        print(json.dumps({
            "shipped": shipped,
            "requested_agent": args.agent_id,
            "matched_agent": target.get("id") if target else None,
            "would_update": "agent" if target is not None else "defaults",
            "replace_path": replace_path,
        }, indent=2))
        return

    fd, patch_file = tempfile.mkstemp(suffix=".json", prefix="dinomem_wire_skills_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(patch, f, indent=2)

        r = subprocess.run(
            ["openclaw", "config", "patch", "--file", patch_file, "--replace-path", replace_path],
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
