#!/usr/bin/env python3
"""
hook_tool.py — Safe scaffolder for agent hooks (dinomem).
LLM classifies intent, picks surface+event, calls this script.
Script handles: 2-stage classify, validate (valid-16 allowlist), scaffold from template,
read-current-state (dedup), confirm, apply via `openclaw hooks`.
NEVER hand-edits openclaw.json. LLM fills template blanks only, never writes handler.ts from scratch.

Usage (CLI):
  hook_tool.py scaffold <name> --event <EVENT> --emoji <e> --desc <text> \\
      [--gate <ts-snippet>] [--action <ts-snippet>] [--requires-bins b1,b2] [--confirmed]
  hook_tool.py list [--name <substr>]
  hook_tool.py remove <name> [--confirmed]

## ROUTING MAP (classify intent -> surface+event; semantic, not keyword; multilingual)

STAGE A — surface (power classification; decide FIRST):
  react_only side-effect (log, snapshot, inject, call API) -> INTERNAL hook (this tool)
  must BLOCK | CANCEL | REWRITE (veto tool, drop msg, mutate prompt) -> TYPED plugin hook (api.on; NOT this tool -> emit guidance, do not scaffold)
  telemetry_only / observability -> diagnostic event (NOT a hook)

STAGE B — event (closed set of 16; reject anything else):
  session_or_agent_start | inject_at_start       -> agent:bootstrap
  on_/new | /reset | /stop                       -> command:new | command:reset | command:stop
  any_command                                    -> command
  before/after compaction                        -> session:compact:before | session:compact:after
  session_props_change                           -> session:patch
  inbound_message                                -> message:received
  after_transcription                            -> message:transcribed
  after_media_link_prep                          -> message:preprocessed
  outbound_delivered                             -> message:sent
  gateway boot | shutdown | before_restart       -> gateway:startup | gateway:shutdown | gateway:pre-restart

## COST (T0/T1; internal hooks are Node -> mostly T0 = deterministic, no LLM)
  put a DETERMINISTIC gate FIRST in the handler; return early on no-work.
  only escalate to an LLM (via execFile of a tool) on a real hit -> keep the LLM off the hot path.

## CONFIRM (all hooks change runtime behavior)
  scaffold: confirm before enabling (--confirmed)
  removal:  confirm (--confirmed)

## REPLYABLE SURFACES (event.messages delivered only here)
  command:* , message:received  -> messages delivered
  agent:bootstrap, session:*, gateway:*, message:sent -> pushed messages IGNORED

## APPLY (native only)
  scaffold writes <workspace>/hooks/<name>/{HOOK.md,handler.ts} ; then `openclaw hooks enable <name>` ; restart may be needed.
  NEVER hand-edit openclaw.json.
"""
import json
import os
import re
import subprocess
import sys

VALID_EVENTS = {
    "command:new", "command:reset", "command:stop", "command",
    "session:compact:before", "session:compact:after", "session:patch",
    "agent:bootstrap",
    "gateway:startup", "gateway:shutdown", "gateway:pre-restart",
    "message:received", "message:transcribed", "message:preprocessed", "message:sent",
}
# events where event.messages is actually delivered
REPLYABLE = {"command:new", "command:reset", "command:stop", "command", "message:received"}

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(os.path.dirname(HERE), "templates", "hook.handler.ts.tmpl")


def _fail(msg):
    print(json.dumps({"ok": False, "error": msg}))
    sys.exit(1)


def _ok(msg, **extra):
    print(json.dumps({"ok": True, "result": msg, **extra}))
    sys.exit(0)


def _run(argv):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=60)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except Exception as e:  # noqa: BLE001
        return 1, "", str(e)


def _workspace_dir():
    return (
        os.environ.get("OPENCLAW_WORKSPACE")
        or os.environ.get("DINOMEM_WORKSPACE")
        or os.getcwd()
    )


def _hooks_dir():
    return os.path.join(_workspace_dir(), "hooks")


def _split_event(event):
    # "message:received" -> ("message","received"); "command" -> ("command","")
    if ":" in event:
        t, a = event.split(":", 1)
        return t, a
    return event, ""


def _hook_exists(name):
    d = os.path.join(_hooks_dir(), name)
    return os.path.isdir(d)


def cmd_scaffold(args):
    name = args.get("name")
    if not name or not re.match(r"^[a-z][a-z0-9-]*$", name):
        _fail("name must be lowercase letters/digits/hyphens, start with a letter")

    event = args.get("event")
    if event not in VALID_EVENTS:
        _fail(f"--event must be one of the 16 valid events: {sorted(VALID_EVENTS)}")

    if not args.get("confirmed"):
        _fail("scaffolding+enabling a hook changes runtime behavior — re-run with --confirmed after user approval")

    if _hook_exists(name):
        _fail(f"hook '{name}' already exists at hooks/{name} — remove first, never overwrite")

    desc = args.get("desc") or f"{name} hook for {event}"
    emoji = args.get("emoji") or "🔗"
    etype, eaction = _split_event(event)

    bins = []
    if args.get("requires_bins"):
        bins = [b for b in re.split(r"[ ,]+", args["requires_bins"]) if b]

    # read template
    try:
        with open(TEMPLATE, "r", encoding="utf-8") as f:
            tmpl = f.read()
    except Exception as e:  # noqa: BLE001
        _fail(f"template not found ({TEMPLATE}): {e}")

    gate = args.get("gate") or "// TODO: deterministic gate; e.g. if (!context.from) return;"
    action = args.get("action") or "// TODO: side effect; e.g. console.log('[' + " + json.dumps(name) + " + '] fired');"

    handler = (
        tmpl.replace("{{HOOK_NAME}}", name)
        .replace("{{EVENT_TYPE}}", etype)
        .replace("{{EVENT_ACTION}}", eaction)
        .replace("{{EVENT}}", event)
        .replace("{{GATE_LOGIC}}", gate)
        .replace("{{ACTION_LOGIC}}", action)
    )

    meta = {"openclaw": {"emoji": emoji, "events": [event], "requires": {"bins": bins}}}
    hook_md = (
        "---\n"
        f"name: {name}\n"
        f'description: "{desc}"\n'
        "metadata:\n"
        f"  {json.dumps(meta, ensure_ascii=False)}\n"
        "---\n\n"
        f"# {name}\n\n"
        f"{desc}\n\n"
        f"Event: `{event}`"
        + ("" if event in REPLYABLE else "  \n(lifecycle event — pushed `event.messages` are IGNORED here)")
        + "\n"
    )

    d = os.path.join(_hooks_dir(), name)
    try:
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "handler.ts"), "w", encoding="utf-8") as f:
            f.write(handler)
        with open(os.path.join(d, "HOOK.md"), "w", encoding="utf-8") as f:
            f.write(hook_md)
    except Exception as e:  # noqa: BLE001
        _fail(f"scaffold write failed: {e}")

    rc, out, err = _run(["openclaw", "hooks", "enable", name])
    enabled = rc == 0
    note = None
    if not enabled:
        note = f"scaffolded but enable failed ({err or out}); run: openclaw hooks enable {name}"
    _ok(
        f"hook '{name}' scaffolded for {event}" + (" + enabled" if enabled else ""),
        path=f"hooks/{name}",
        replyable=event in REPLYABLE,
        enabled=enabled,
        note=note,
    )


def cmd_list(args):
    rc, out, err = _run(["openclaw", "hooks", "list", "--json"])
    if rc != 0:
        # fall back to filesystem
        try:
            names = sorted(os.listdir(_hooks_dir()))
        except Exception:  # noqa: BLE001
            names = []
        sub = args.get("name")
        if sub:
            names = [n for n in names if sub in n]
        _ok(f"{len(names)} hook dir(s)", hooks=names, source="fs")
        return
    _ok("hooks listed", raw=out, source="cli")


def cmd_remove(args):
    name = args.get("name")
    if not args.get("confirmed"):
        _fail("removing a hook changes runtime behavior — re-run with --confirmed after user approval")
    # disable via CLI (best-effort), then remove the dir
    _run(["openclaw", "hooks", "disable", name])
    d = os.path.join(_hooks_dir(), name)
    if not os.path.isdir(d):
        _fail(f"hook '{name}' not found at hooks/{name}")
    try:
        import shutil
        shutil.rmtree(d)
    except Exception as e:  # noqa: BLE001
        _fail(f"remove failed: {e}")
    _ok(f"hook '{name}' disabled + removed")


def _parse_argv(argv):
    if not argv:
        _fail("usage: hook_tool.py scaffold|list|remove ...")
    cmd = argv[0]
    args = {}
    i = 1
    positional = []
    while i < len(argv):
        a = argv[i]
        if a.startswith("--"):
            key = a[2:].replace("-", "_")
            if key == "confirmed":
                args[key] = True
                i += 1
            else:
                if i + 1 >= len(argv):
                    _fail(f"flag {a} needs a value")
                args[key] = argv[i + 1]
                i += 2
        else:
            positional.append(a)
            i += 1
    if positional:
        args["name"] = positional[0]
    return cmd, args


def main():
    cmd, args = _parse_argv(sys.argv[1:])
    if cmd == "scaffold":
        cmd_scaffold(args)
    elif cmd == "list":
        cmd_list(args)
    elif cmd == "remove":
        if "name" not in args:
            _fail("remove requires a <name>")
        cmd_remove(args)
    else:
        _fail(f"unknown command '{cmd}' (scaffold|list|remove)")


if __name__ == "__main__":
    main()
