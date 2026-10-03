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

1. **Local per-agent index warm** (mandatory): For every agent in the running gateway's own
   `cfg.agents.list`, fire-and-forget launches `openclaw memory search "warmup" --agent <id>`
   detached, output to `<workspace>/logs/memory_warm.log` (or ignored if unwritable).
   Each launch is independent; one agent's failure never affects another. The query string is a
   fixed dummy (`"warmup"`) — results are never read.

2. **Shared TEI embedding warm** (mandatory): Once per gateway process, fire a direct HTTP POST
   to the resolved TEI `/embeddings` endpoint with a dummy input. This is more efficient on
   multi-agent gateways (N agents + 1 TEI probe, not N × (N+1)), and harmless on setups where
   multiple gateways share one TEI instance.

3. Returns immediately. Never blocks the gateway startup path.

## Scope / configuration

### Mandatory by default

Warming is **mandatory by default**: every agent in the running gateway's config gets warmed
automatically, no env var opt-in required. The hook resolves agents and TEI config from
the gateway's own running config.

### Optional power-user overrides

For advanced setups or non-standard embedding URLs, two env vars can override the config-based
resolution:

```bash
# Override TEI URL (e.g., a remote embeddings server instead of localhost:8080):
DINOMEM_TEI_URL=https://embeddings.example.com/v1

# Override TEI model id:
DINOMEM_TEI_MODEL=sentence-transformers/all-MiniLM-L6-v2
```

Both must be set together to take effect; if either is unset, the hook falls back to the
config-based resolution. These are NOT required for normal operation — omit them unless you
have a specific non-standard deployment.

## Requirements

- `openclaw` on PATH (the hook shells `openclaw memory search`).
- A memory_search-enabled agent (dinomem/base default).

## Enable

```bash
openclaw hooks enable dinomem-memory-warm
openclaw gateway restart
```
