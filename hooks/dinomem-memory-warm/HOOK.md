---
name: dinomem-memory-warm
description: "On gateway startup, fire one throwaway memory_search per configured agent so the first REAL query lands warm (~0.4s) instead of paying the one-time cold-boot spike (~6s: model load + FTS/vector handle open + embedding-cache seed). Fire-and-forget, never blocks boot."
metadata:
  { "openclaw": { "emoji": "🔥", "events": ["gateway:startup"], "requires": { "bins": ["openclaw"] } } }
---

# dinomem-memory-warm

Pre-warm memory_search after every gateway restart.

## Why

`memory_search` is sub-second once warm (measured ~0.4s: `searchMs 405`), but the
**first** call after a gateway restart pays a one-time cold cost (~6s: embedding model
load + FTS5/vector handle open + embedding-cache seed). That cold spike lands on whatever
real query happens to be first — usually a user waiting on an answer, and on a large corpus
it can even trip the tool's 15s timeout + 60s failure-cooldown, making it look broken.

This hook fires one **throwaway** `memory_search` per configured agent the instant the
gateway is up, in the background. It absorbs the cold cost against a dummy query so the
user's first real query is already warm. Strictly an improvement, never a regression: if the
warmup fails or is slow, nothing user-facing is affected — it's detached and its result is
discarded.

## What it does

On `gateway:startup`:

1. Resolves the agent list to warm from `DINOMEM_WARM_AGENTS` (comma-separated agent ids).
   If unset, does nothing (opt-in — no accidental host-wide warming).
2. For each agent id, fire-and-forget launches
   `openclaw memory search "warmup" --agent <id>` detached, output to
   `<workspace>/logs/memory_warm.log` (or ignored if unwritable).
3. Returns immediately. Never blocks the gateway startup path.

Each launch is independent; one agent's failure never affects another. The query string is a
fixed dummy (`"warmup"`) — results are never read.

## Scope / configuration

Warming is **opt-in** via env, so a multi-agent host only warms what you choose (each warmed
agent pays one cold search at boot):

```bash
# warm only the heavy-corpus agent (recommended default for most installs):
DINOMEM_WARM_AGENTS=analyst

# or several:
DINOMEM_WARM_AGENTS=analyst,sales
```

Set it in the gateway's systemd env (`~/.openclaw/gateway.systemd.env`) or the agent env so
the hook process inherits it. Unset = hook is a no-op.

## Requirements

- `openclaw` on PATH (the hook shells `openclaw memory search`).
- A memory_search-enabled agent (dinomem/base default).

## Enable

```bash
openclaw hooks enable dinomem-memory-warm
openclaw gateway restart
```
