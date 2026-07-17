#!/usr/bin/env python3
"""
cron_tool.py — Safe writer for agent cron jobs (dinomem).
LLM classifies intent, picks params, calls this script.
Script handles: read-current-state (dedup), validate, cost-gate, apply via `openclaw cron`.
NEVER hand-edits openclaw.json. NEVER emulates timers with sleep/poll.

Usage (CLI):
  cron_tool.py add <name> --schedule <at:ISO|every:DUR|cron:EXPR[@TZ]> --tier <T0|T1|T2|T3> \\
      [--system-event <text>] [--command <sh>] [--message <text>] \\
      [--target main|isolated] [--gate <script[ args]>  (T1: standalone payload, not with --command)] [--model <alias>] [--no-reasoning] \\
      [--delivery none|announce|webhook] [--to <dest>] [--channel <ch>] [--agent <id>] [--confirmed]
  cron_tool.py list [--name <substr>]
  cron_tool.py remove <name> [--confirmed]

## ROUTING MAP (classify intent -> params; semantic, not keyword; multilingual)

AXIS 1 — schedule:
  one_shot | in_N_time | on_date        -> at:ISO      (offset-less -> pair with tz)
  interval | every_N                    -> every:DUR   (10m,1h,2d)
  daily | weekly | wallclock_recurring  -> cron:EXPR@TZ (EXPR = tz wallclock, never hand-convert to UTC)

AXIS 2 — payload + target:
  remind_me | notify | nudge_chat       -> --system-event  (main)      [T0]
  deterministic_check | fetch | alert   -> --command       (main)      [T0/T1]
  do_work | research | draft | classify -> --message       (isolated)  [T2/T3]

AXIS 3 — delivery:
  reply_in_chat   -> announce (--to/--channel)
  post_external   -> webhook  (--to url)
  silent|internal -> none

AXIS 4 — runtime_cost_tier (decide FIRST; pick cheapest meeting goal):
  deterministic_check_or_transform  -> T0  --command | --system-event      no LLM
  judgment_but_not_every_fire       -> T1  --gate (stdout on hit) -> conditional  LLM on hit only
  llm_no_reasoning                  -> T2  --message + cheapest model       STOP: recommend+approve
  llm_reasoning                     -> T3  --message + default model        disclose recurring cost + confirm

## COST RULES (enforced)
  T0 preferred: deterministic goal -> --command/--system-event, never --message.
  no unconditional recurring --message: recurring + message + no gate -> REJECT unless --confirmed (T3).
  model downgrade = approval: T2 requires --no-reasoning + --model (or DINOMEM_CHEAP_MODEL); never auto-picks.

## CONFIRM TIER (writer refuses without --confirmed)
  main + system-event (reminder)   -> direct
  isolated message (agentTurn)     -> confirm
  webhook delivery                 -> confirm
  removal                          -> confirm

## VALIDATE (closed sets; reject others)
  schedule kind in {at, every, cron}
  target in {main, isolated}
  payload: main -> system-event|command ; isolated -> message
  delivery in {none, announce, webhook}
  cron expr = tz wallclock ; tz omitted -> gateway local
  system-event text reads as a reminder when it fires ; include context

## DEDUP (read current state first)
  `openclaw cron list` before add ; same name -> REJECT (update via remove+add), never blind-add (dup double-fires).

## GATE (T1 zero-LLM-on-empty)
  --gate <script>: recurring --command runs gate ; gate prints text ONLY on hit -> systemEvent delivers it, model spends only then ; empty stdout -> no-op, zero model.
  ship gate/ templates: file-changed.sh, threshold.sh, diff-since-last.sh (pure shell, coreutils-only).
"""
import json
import os
import subprocess
import sys

TARGETS = {"main", "isolated"}
TIERS = {"T0", "T1", "T2", "T3"}
DELIVERY = {"none", "announce", "webhook"}


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


def _extract_json(text):
    # CLI may prepend config warnings to stdout; grab the first JSON value.
    for i, ch in enumerate(text):
        if ch in "[{":
            try:
                return json.loads(text[i:])
            except Exception:  # noqa: BLE001
                continue
    return None


def _list_jobs():
    rc, out, _ = _run(["openclaw", "cron", "list", "--json"])
    if rc != 0:
        return []
    data = _extract_json(out)
    if data is None:
        return []
    return data if isinstance(data, list) else data.get("jobs", [])


def _resolve_id(name):
    # Returns (id, exists). Accepts a name or an id.
    for j in _list_jobs():
        if isinstance(j, dict) and (j.get("name") == name or j.get("id") == name):
            return j.get("id"), True
    return None, False


def _name_exists(name):
    _, exists = _resolve_id(name)
    return exists


def _parse_schedule(spec):
    # at:<iso> | every:<dur> | cron:<expr>[@<tz>]
    if ":" not in spec:
        _fail("schedule must be at:<iso> | every:<dur> | cron:<expr>[@tz]")
    kind, rest = spec.split(":", 1)
    kind = kind.strip().lower()
    if kind not in {"at", "every", "cron"}:
        _fail(f"schedule kind must be at|every|cron, got {kind}")
    tz = None
    if kind == "cron" and "@" in rest:
        rest, tz = rest.rsplit("@", 1)
    return kind, rest.strip(), (tz.strip() if tz else None)


def cmd_add(args):
    name = args["name"]
    if _name_exists(name):
        _fail(f"job '{name}' already exists — remove+add to update; never blind-add (dup double-fires)")

    tier = (args.get("tier") or "").upper()
    if tier not in TIERS:
        _fail(f"--tier required, one of {sorted(TIERS)}")

    target = args.get("target")
    if target is not None and target.lower() not in TARGETS:
        _fail(f"--target must be one of {sorted(TARGETS)}")

    delivery = (args.get("delivery") or "none").lower()
    if delivery not in DELIVERY:
        _fail(f"--delivery must be one of {sorted(DELIVERY)}")

    system_event = args.get("system_event")
    command = args.get("command")
    message = args.get("message")
    gate = args.get("gate")
    model = args.get("model")
    no_reasoning = args.get("no_reasoning")
    confirmed = args.get("confirmed")

    # T1: --gate is itself the payload (its stdout on a hit); it does not need --command.
    payloads = [p for p in (system_event, command, message, gate) if p]
    if len(payloads) != 1:
        _fail("exactly one payload: --system-event | --command | --message | --gate")

    # target is INFERRED from payload (systemEvent->main, message->isolated, command/gate->n/a).
    # An explicit --target that contradicts the payload is a user error.
    if target is not None:
        t = target.lower()
        if system_event and t != "main":
            _fail("system-event is a main-session job; --target must be main or omitted")
        if message and t != "isolated":
            _fail("message is an isolated agentTurn; --target must be isolated or omitted")
        if (command or gate) and t:
            pass  # command/gate ignore --target (command-type job, no session)

    kind, val, tz = _parse_schedule(args["schedule"])
    recurring = kind in {"every", "cron"}

    # COST GATE
    if message:  # T2/T3 path
        if recurring and not confirmed:
            _fail("recurring agentTurn (message) is expensive per-fire — needs --confirmed (T3) or convert to --command --gate (T1)")
        if tier == "T2":
            if not no_reasoning:
                _fail("T2 = llm_no_reasoning: pass --no-reasoning")
            if not model and not os.environ.get("DINOMEM_CHEAP_MODEL"):
                _fail("T2 requires a cheap model: pass --model <alias> or set DINOMEM_CHEAP_MODEL (approval-gated, never auto)")
    if tier == "T1" and not gate:
        _fail("T1 = deterministic-gate: pass --gate <script>; without a gate this is T0 or T3")
    if gate and command:
        _fail("--gate is itself the payload; do not also pass --command (gate stdout on a hit IS the message)")

    # CONFIRM TIER: agentTurn (message) or webhook egress require approval.
    needs_confirm = bool(message) or (delivery == "webhook")
    if needs_confirm and not confirmed:
        _fail("this job changes behavior (agentTurn / webhook egress) — re-run with --confirmed after user approval")

    # BUILD CLI
    argv = ["openclaw", "cron", "add", "--name", name]
    if kind == "at":
        argv += ["--at", val]
    elif kind == "every":
        argv += ["--every", val]
    else:
        argv += ["--cron", val]
        if tz:
            argv += ["--tz", tz]

    # Job type dictates --session: systemEvent/agentTurn are session jobs; command/gate are command jobs (no --session).
    is_command_job = bool(gate or command)

    if system_event:
        argv += ["--session", "main", "--system-event", system_event]
    elif gate:
        # T1: run the gate; its stdout is delivered ONLY on a hit (empty stdout -> nothing to announce, zero model).
        # gate is executed as shell -> TRUSTED (developer-supplied) input only.
        shell = f'out=$({gate}); [ -n "$out" ] && printf "%s" "$out"; exit 0'
        argv += ["--command", shell]
    elif command:
        argv += ["--command", command]
    else:  # message -> agentTurn
        argv += ["--session", "isolated", "--message", message]
        chosen_model = model or os.environ.get("DINOMEM_CHEAP_MODEL")
        if chosen_model and tier == "T2":
            argv += ["--model", chosen_model]
        if no_reasoning:
            argv += ["--thinking", "off"]
        # Multi-agent installs need an explicit target agent; pass through, never hardcode.
        if args.get("agent"):
            argv += ["--agent", args["agent"]]

    # Delivery. Command jobs (gate/command) reach chat only via --announce, so a
    # command job with default delivery still announces its stdout (silent on empty).
    eff_delivery = delivery
    if is_command_job and delivery == "none":
        eff_delivery = "announce"
    if eff_delivery == "announce":
        argv += ["--announce"]
        if args.get("to"):
            argv += ["--to", args["to"]]
        if args.get("channel"):
            argv += ["--channel", args["channel"]]
    elif eff_delivery == "webhook":
        if not args.get("to"):
            _fail("webhook delivery requires --to <url>")
        argv += ["--webhook", args["to"]]

    argv += ["--json"]
    rc, out, err = _run(argv)
    if rc != 0:
        _fail(f"cron add failed: {err or out}")
    notes = []
    if eff_delivery == "announce" and not args.get("channel") and not args.get("to"):
        notes.append("delivery uses the last-used channel; on multi-channel installs pass --channel <id> (or --to) or delivery may be ambiguous")
    if message and not args.get("agent"):
        notes.append("agentTurn job has no --agent; on multi-agent installs it runs as the default agent — pass --agent <id> to target a specific agent")
    note = "; ".join(notes) if notes else None
    _ok(f"cron job '{name}' created (tier={tier}, schedule={kind})", cli=out, note=note)


def cmd_list(args):
    jobs = _list_jobs()
    sub = args.get("name")
    if sub:
        jobs = [j for j in jobs if isinstance(j, dict) and sub.lower() in str(j.get("name", "")).lower()]
    _ok(f"{len(jobs)} job(s)", jobs=jobs)


def cmd_remove(args):
    name = args["name"]
    if not args.get("confirmed"):
        _fail("removal changes behavior — re-run with --confirmed after user approval")
    job_id, exists = _resolve_id(name)
    if not exists:
        _fail(f"job '{name}' not found")
    rc, out, err = _run(["openclaw", "cron", "rm", job_id])
    if rc != 0:
        _fail(f"cron rm failed: {err or out}")
    _ok(f"cron job '{name}' removed")


def _parse_argv(argv):
    if not argv:
        _fail("usage: cron_tool.py add|list|remove ...")
    cmd = argv[0]
    args = {}
    i = 1
    positional = []
    while i < len(argv):
        a = argv[i]
        if a.startswith("--"):
            key = a[2:].replace("-", "_")
            if key in {"no_reasoning", "confirmed"}:
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
    if cmd == "add":
        if "name" not in args:
            _fail("add requires a <name>")
        if "schedule" not in args:
            _fail("add requires --schedule at:<iso>|every:<dur>|cron:<expr>[@tz]")
        cmd_add(args)
    elif cmd == "list":
        cmd_list(args)
    elif cmd == "remove":
        if "name" not in args:
            _fail("remove requires a <name>")
        cmd_remove(args)
    else:
        _fail(f"unknown command '{cmd}' (add|list|remove)")


if __name__ == "__main__":
    main()
