---
name: self-config
description: Change the agent's own behavior, rules, workflows, persona, tools, or preferences by routing edits to the right bootstrap file via dinomem's config_tool.py. Read this when the user implies modifying how the agent works or who it is.
---

# Self-config (dinomem)

When the user asks to change the agent's behavior, rules, workflow, persona,
tools, or preferences, route the edit to the correct workspace bootstrap file
using `tools/config_tool.py`.

## When to use

The user implies changing behavior / rules / workflows / persona / tools /
preferences — e.g. "stop doing X", "always Y", "call me Z", "add a tool for…",
"change your tone".

## How

1. **Read the routing map:** open `tools/config_tool.py` and read its docstring
   — it maps intents to the target file (SOUL.md / IDENTITY.md / AGENTS.md /
   TOOLS.md / USER.md).
2. **Generate the content** for that file.
3. **Call `config_tool.py`** to apply it.

## Confirm-before-write

| Files | Policy |
| ----- | ------ |
| `SOUL.md`, `IDENTITY.md`, `AGENTS.md` | **Confirm with the user before writing** — these change core behavior/persona. |
| `TOOLS.md`, `USER.md` | Write directly, no confirmation needed. |

If the intent is ambiguous about which file/behavior to change, **ask one
clarifying question**, then route.
