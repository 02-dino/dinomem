---
name: dinomem-reset-extract
description: "On manual /new or /reset, run the dinomem memory pipeline immediately (adopt reset-archive + extract + optional ingest) instead of waiting for the next cron tick — closes the post-reset memory-blindness window to ~0."
metadata:
  { "openclaw": { "emoji": "🧠", "events": ["command:new", "command:reset"], "requires": { "bins": ["python3"] } } }
---

# dinomem-reset-extract

Zero-delay memory extraction on manual session reset.

## Why

The dinomem memory pipeline (`procedures/auto_session_reset.py`) normally runs on a
`*/15` cron. It: adopts OpenClaw core's real-time reset-archives
(`<session>.jsonl.reset.<iso>Z`) into the pipeline-visible namespace, runs
`extract_memory.py`, then (if the neuron layer is installed) `session_ingest.py`.

When you manually `/new` or `/reset`, OpenClaw resets the session **immediately**, but
extraction only catches up on the next cron tick — up to ~15 minutes later. During that
window the just-ended session's memory is not yet in `memory/`, so a fresh question that
depends on it comes up empty.

This hook fires the instant you issue `/new` or `/reset` and runs the **same** pipeline
right then, in the background. Result: the memory of the session you just left is extracted
before the new session needs it. If the hook races core's archive rename and misses, the
regular cron still catches it on the next tick — so this is strictly an improvement, never a
regression.

## What it does

On `command:new` / `command:reset`:

1. Resolves the workspace dir (from the event context, falling back to
   `OPENCLAW_WORKSPACE` / `DINOMEM_WORKSPACE`).
2. Fire-and-forget launches `python3 <workspace>/procedures/auto_session_reset.py`.
   That script self-serializes via `/tmp/dinomem_auto_reset.lock` (so it can't collide
   with the cron run) and is fully idempotent (per-archive processed-log + content-hash
   dedup), so a concurrent cron tick is harmless.

The handler never blocks the `/new` / `/reset` acknowledgement: heavy work is detached and
the handler returns immediately.

## Requirements

- `python3` on PATH.
- A dinomem install in the target workspace (`procedures/auto_session_reset.py` present).

## Enable

```bash
openclaw hooks enable dinomem-reset-extract
openclaw gateway restart
```
