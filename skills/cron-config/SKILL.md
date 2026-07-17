---
name: cron-config
description: Create, list, or remove agent cron jobs (reminders, checks, recurring agent work) by routing intent to dinomem's cron_tool.py. Read this when the user implies scheduling, reminding, recurring checks, or automation timing.
---

# Cron-config (dinomem)

When the user asks to schedule something — a reminder, a recurring check, a
delayed follow-up, automation on a timer — route the request through
`tools/cron_tool.py`. Never hand-edit `openclaw.json`, never emulate timers
with sleep/poll loops.

## When to use

The user implies scheduling / reminding / recurring work / delayed
follow-up / "every day/hour", "remind me", "check X periodically", "stop
that cron", "cancel the reminder".

## How

1. **Read the routing map:** open `tools/cron_tool.py` and read its
   docstring — it defines the 4 classification axes (schedule kind,
   payload+target, delivery, runtime cost tier) and validation rules.
2. **Classify runtime cost tier FIRST** (T0-T3, cheapest that meets the
   goal) — this decides whether the job needs a gate script, a cheap model,
   or full agent reasoning.
3. **Check for an existing job with the same name** via
   `cron_tool.py list` before adding — never blind-add (duplicates double-fire).
4. **Call `cron_tool.py add/list/remove`** with the classified params.

## Confirm-before-write

| Job type | Policy |
| -------- | ------ |
| `main` + system-event (reminder) | Direct, no confirmation |
| `isolated` + message (agentTurn) | **Confirm with the user first** |
| webhook delivery | **Confirm with the user first** |
| removal (stop/cancel/delete) | **Confirm with the user first** |

Recurring `--message` (agentTurn) jobs without a gate are expensive per-fire
and are refused by the tool unless `--confirmed` is passed — prefer
converting to `--command --gate <script>` (T1) when the work is a
deterministic check that only occasionally needs LLM judgment.

If schedule, target, or tier is ambiguous, **ask one clarifying question**,
then route.
