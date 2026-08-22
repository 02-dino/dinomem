---
name: self-config
description: Change the agent's own behavior, rules, workflows, persona, tools, or preferences by routing edits to the right bootstrap file via dinomem's config_tool.py. Read this when the user implies modifying how the agent works or who it is.
---

# Self-config (dinomem)

When the user asks to change the agent's behavior, rules, workflow, persona,
tools, or preferences, route the edit to the correct workspace bootstrap file
using `tools/config_tool.py`.

## Route first

Run `tools/route.py classify` and confirm the arbiter selected **root** (discriminators 4-7).
A schedule -> cron-config; a gateway event -> hook-config; on-demand procedure -> skill-config.
Root files load EVERY turn — AGENTS.md is the LAST resort. Only unconditional, always-on config lands here.

## When to use

The user implies changing behavior / rules / workflows / persona / tools /
preferences — e.g. "stop doing X", "always Y", "call me Z", "add a tool for…",
"change your tone".

## How

> Writing more than a few lines of new code (a rule block, a helper)? Read the
> **build-quality** skill first — small, DRY, reused, documented, tested.

1. **Read the routing map:** open `tools/config_tool.py` and read its docstring
   — it maps intents to the target file (SOUL.md / IDENTITY.md / AGENTS.md /
   TOOLS.md / USER.md).
2. **Generate the content** for that file.
3. **Call `config_tool.py`** to apply it.
4. **Verify it landed (don't assume):** run
   `tools/route.py verify root "<a unique phrase from what you wrote>" --file <TARGET.md>`
   — exit 0 = present, exit 1 = missing/failed. If it failed (or the target was a
   forbidden managed file), the write did NOT stick: fix and re-verify, don't
   report success. This is the mechanized post-condition for the route (same
   test-don't-assume rule the arbiter is built on).

## Confirm-before-write

| Files | Policy |
| ----- | ------ |
| `SOUL.md`, `IDENTITY.md`, `AGENTS.md` | **Confirm with the user before writing** — these change core behavior/persona. |
| `TOOLS.md`, `USER.md` | Write directly, no confirmation needed. |

If the intent is ambiguous about which file/behavior to change — OR more than
one target file/behavior is a plausible reading — **surface the fork, don't pick
silently**: name the interpretations, ask one clarifying question, then route.
A wrong-target root write is always-on and costly to unwind.
