#!/usr/bin/env python3
"""
skill_tool.py — Safe scaffolder for agent skills (dinomem).
LLM classifies intent, generates name+description+body, calls this script.
Script handles: slug validation, backup, dedup (existing-skill check), scaffold
SKILL.md (frontmatter + body), thin AGENTS.md when_to_use trigger, apply via
`openclaw skills install <dir>`. NEVER hand-edits openclaw.json.

Usage (CLI):
  skill_tool.py scaffold <slug> --name <name> --desc <description> --body <markdown> [--trigger <one-line>] [--confirmed]
  skill_tool.py list [--slug <substr>]
  skill_tool.py remove <slug> [--confirmed]

## ROUTING MAP (when a request belongs to SKILL, not another surface)
A skill = procedural knowledge / a multi-step method the agent reads ON-DEMAND when a
specific task class appears. Not always-on. Route here (from route.py discriminator 3) when:
  workflow | checklist | how_to | domain_procedure | method_only_needed_sometimes -> skill
NOT a skill:
  runs on a schedule            -> cron_tool.py
  reacts to a gateway event     -> hook_tool.py
  always-true rule/identity/pref-> config_tool.py (root file)

## TRIGGER vs BODY (a skill is NOT fully root-free)
  description frontmatter = the PRIMARY trigger (agent reads SKILL.md when description matches).
  --trigger writes ONE extra line to AGENTS.md when_to_use for a hard, always-visible pointer.
    Use --trigger only when the description alone is too weak to fire reliably. Keep it ONE line.
    Never inline the skill BODY into AGENTS.md — body stays in SKILL.md, loaded on-demand.

## WRITING PRINCIPLES (for the body the LLM generates)
  description: <=1 sentence, states WHEN to read + WHAT it gives. This is the trigger surface.
  body: machine-readable, imperative, no filler. Steps/rules the agent follows for that task.
  no examples unless they change behavior. no history. no meta-commentary.

## CONFIRM (skills change agent capability)
  scaffold: confirm before install (--confirmed)
  removal:  confirm (--confirmed)

## APPLY (native only)
  scaffold writes <workspace>/skills/<slug>/SKILL.md, then `openclaw skills install <dir>`.
  NEVER hand-edit openclaw.json.
"""
import argparse
import json
import os
import re
import subprocess
from pathlib import Path

_WS_DEFAULT = "DINOMEM_WORKSPACE_PLACEHOLDER"
if _WS_DEFAULT.startswith("DINOMEM_"):
    _WS_DEFAULT = str(Path(__file__).resolve().parent.parent)
WORKSPACE = Path(os.environ.get("DINOMEM_WORKSPACE", _WS_DEFAULT))
SKILLS_DIR = WORKSPACE / "skills"
AGENTS_FILE = WORKSPACE / "AGENTS.md"
BACKUP_SCRIPT = WORKSPACE.parent.parent / "scripts/file-backup.sh"

SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{1,63}$")

def _backup(path):
    if BACKUP_SCRIPT.exists() and Path(path).exists():
        subprocess.run([str(BACKUP_SCRIPT), str(path)], capture_output=True)

def _valid(text):
    return "\x00" not in text and len(text) <= 50_000

def _skill_dir(slug):
    return SKILLS_DIR / slug

def scaffold(slug, name, desc, body, trigger=None, confirmed=False):
    if not SLUG_RE.match(slug):
        return {"ok": False, "error": f"Invalid slug '{slug}'. Use lowercase [a-z0-9-], start with a letter, 2-64 chars."}
    if not (name and desc and body):
        return {"ok": False, "error": "name, desc, and body are all required"}
    for t in (name, desc, body, trigger or ""):
        if not _valid(t):
            return {"ok": False, "error": "content failed validation (null bytes or too large)"}
    d = _skill_dir(slug)
    skill_md = d / "SKILL.md"
    if skill_md.exists() and not confirmed:
        return {"ok": False, "error": f"skill '{slug}' exists. Re-run with --confirmed to overwrite.", "needs_confirm": True}
    if not confirmed:
        return {"ok": False, "needs_confirm": True,
                "preview": {"slug": slug, "name": name, "desc": desc, "trigger": trigger},
                "note": "Skills change agent capability. Re-run with --confirmed to install."}
    d.mkdir(parents=True, exist_ok=True)
    if skill_md.exists():
        _backup(skill_md)
    content = f"---\nname: {name}\ndescription: {desc.strip()}\n---\n\n{body.strip()}\n"
    skill_md.write_text(content, encoding="utf-8")

    trigger_written = None
    if trigger:
        trigger_written = _append_trigger(slug, trigger)

    # Apply natively (best-effort; scaffolding on disk already makes it discoverable).
    applied, apply_msg = _install(d)
    return {"ok": True, "slug": slug, "path": str(skill_md), "action": "scaffold",
            "trigger_written": trigger_written, "installed": applied, "apply": apply_msg}

def _append_trigger(slug, trigger):
    """Add ONE line to AGENTS.md when_to_use pointing at the skill. Idempotent."""
    line = f"  {slug}: {trigger.strip()}"
    existing = AGENTS_FILE.read_text(encoding="utf-8") if AGENTS_FILE.exists() else ""
    if f"{slug}:" in existing and trigger.strip() in existing:
        return "skip:duplicate"
    _backup(AGENTS_FILE)
    if "## skill_triggers" in existing:
        out = re.sub(r"(## skill_triggers\n)", r"\1" + line + "\n", existing, count=1)
    else:
        block = "\n## skill_triggers\n" + line + "\n"
        out = (existing.rstrip("\n") + "\n" + block) if existing else block.lstrip("\n")
    AGENTS_FILE.write_text(out, encoding="utf-8")
    return "appended"

def _install(skill_dir):
    try:
        r = subprocess.run(["openclaw", "skills", "install", str(skill_dir), "--force"],
                           capture_output=True, text=True, timeout=120)
        ok = r.returncode == 0
        return ok, (r.stdout or r.stderr or "").strip()[-400:]
    except Exception as e:
        return False, f"install-skipped: {e} (skill is on disk; run `openclaw skills install {skill_dir}`)"

def list_skills(substr=None):
    if not SKILLS_DIR.exists():
        return {"ok": True, "skills": []}
    out = []
    for p in sorted(SKILLS_DIR.iterdir()):
        md = p / "SKILL.md"
        if not md.exists():
            continue
        if substr and substr.lower() not in p.name.lower():
            continue
        desc = ""
        for ln in md.read_text(encoding="utf-8").splitlines()[:8]:
            if ln.startswith("description:"):
                desc = ln.split(":", 1)[1].strip()
                break
        out.append({"slug": p.name, "description": desc})
    return {"ok": True, "skills": out}

def remove(slug, confirmed=False):
    d = _skill_dir(slug)
    if not (d / "SKILL.md").exists():
        return {"ok": False, "error": f"skill '{slug}' not found"}
    if not confirmed:
        return {"ok": False, "needs_confirm": True, "note": f"Re-run with --confirmed to remove skill '{slug}'."}
    _backup(d / "SKILL.md")
    import shutil
    shutil.rmtree(d)
    # Best-effort: strip its trigger line from AGENTS.md
    if AGENTS_FILE.exists():
        txt = AGENTS_FILE.read_text(encoding="utf-8")
        new = "\n".join(l for l in txt.splitlines() if not l.strip().startswith(f"{slug}:"))
        if new != txt:
            _backup(AGENTS_FILE)
            AGENTS_FILE.write_text(new + "\n", encoding="utf-8")
    return {"ok": True, "slug": slug, "action": "remove"}

def main():
    p = argparse.ArgumentParser(description="Safe scaffolder for agent skills (dinomem)")
    sub = p.add_subparsers(dest="cmd")
    s = sub.add_parser("scaffold")
    s.add_argument("slug")
    s.add_argument("--name", required=True)
    s.add_argument("--desc", required=True)
    s.add_argument("--body", required=True)
    s.add_argument("--trigger", default=None)
    s.add_argument("--confirmed", action="store_true")
    s = sub.add_parser("list")
    s.add_argument("--slug", default=None)
    s = sub.add_parser("remove")
    s.add_argument("slug")
    s.add_argument("--confirmed", action="store_true")
    args = p.parse_args()
    if args.cmd == "scaffold":
        print(json.dumps(scaffold(args.slug, args.name, args.desc, args.body, args.trigger, args.confirmed)))
    elif args.cmd == "list":
        print(json.dumps(list_skills(args.slug)))
    elif args.cmd == "remove":
        print(json.dumps(remove(args.slug, args.confirmed)))
    else:
        p.print_help()

if __name__ == "__main__":
    main()
