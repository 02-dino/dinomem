---
name: hook-config
description: Create, list, or remove agent event hooks (run logic on gateway events — session start, inbound message, command, compaction, gateway lifecycle) by routing intent to dinomem's hook_tool.py. Read this when the user implies reacting to an event, "every time X happens", or automating on a lifecycle event.
---

# Hook-config (dinomem)

When the user asks to run logic when something happens inside the gateway —
"every time a session starts", "when a message comes in", "log every /reset",
"on gateway restart" — route it through `tools/hook_tool.py`. Never hand-edit
`openclaw.json`.

## Route first

Run `tools/route.py classify` and confirm the arbiter selected **hook** (discriminator 2).
A schedule -> cron-config; on-demand procedure -> skill-config; always-on rule -> self-config.

## When to use

The user implies reacting to a gateway event / lifecycle moment — "every time",
"when X happens", "on /new", "before compaction", "log inbound", "at bootstrap".

## How

1. **Read the routing map:** open `tools/hook_tool.py` and read its docstring —
   it defines Stage A (surface) and Stage B (the 16 valid events).
2. **Stage A — pick the surface FIRST:**
   - react-only side effect (log, snapshot, inject, call API) → **internal
     hook** → scaffold via this tool.
   - must **block / cancel / rewrite** (veto a tool, drop a message, mutate the
     prompt) → **typed plugin hook** (`api.on`) → this tool does NOT scaffold
     it; tell the user it needs a typed plugin hook.
   - telemetry-only → diagnostic event, not a hook.
3. **Stage B — pick the event** from the closed set of 16 (reject anything else).
4. **Keep it deterministic:** internal hooks are Node — put a cheap gate first,
   return early on no-work, escalate to an LLM only on a real hit.
5. **Fill the template blanks only** — never write `handler.ts` from scratch.
   Provide `--gate` / `--action` TypeScript snippets; the tool scaffolds the
   vetted template.
6. **Call `hook_tool.py scaffold/list/remove`**.

## Confirm-before-write

| Action | Policy |
| ------ | ------ |
| scaffold + enable a hook | **Confirm with the user first** — all hooks change runtime behavior |
| removal | **Confirm with the user first** |

`event.messages` is delivered only on replyable surfaces (`command:*`,
`message:received`); on `agent:bootstrap`, `session:*`, `gateway:*`,
`message:sent` pushed messages are ignored — the tool flags this per hook.

After scaffolding, enabling may require `openclaw hooks enable <name>` and a
gateway restart. If surface or event is ambiguous, **ask one clarifying
question**, then route.
