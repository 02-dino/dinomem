---
name: context-inject
description: "On message:received, when the inbound EXPLICITLY names a file path or a backtick-wrapped code symbol, front-load that context once (git diff of the file + code_query explain of the symbol) into the model's turn — an on-demand, zero-LLM-gated mimic of an IDE auto-injecting the open file + symbol graph. Non-code messages inject nothing (zero added tokens)."
metadata:
  { "openclaw": { "emoji": "📎", "events": ["message:received"], "requires": { "bins": ["git"] } } }
---

# context-inject

On-demand file/symbol context injection — the cheap, conditional answer to the
IDE "auto-inject the open file + diagnostics" gap. NOT always-on.

## Why (and why it's cheap)

An IDE injects the open file + LSP diagnostics into **every** prompt. In a
chat-agent that is pure waste — most turns aren't code turns. This hook does the
same value **only when it matters**, gated by a **zero-LLM regex**:

- Inbound names an explicit **path** (has a `/` or a code extension like
  `foo.py`, `scripts/verify.sh`) or a **backtick-wrapped symbol**
  (`` `resolveWorkspaceDir` ``) → inject once.
- Inbound is prose ("hi", a market question) → the gate matches nothing →
  **zero added tokens, no model call.**

On a code turn the injection isn't even *extra* cost — it front-loads a `git
diff` + symbol read the model would have done manually anyway, so it's
~cost-neutral. The whole cost story rides on the trigger being **tight**: it
fires on real paths/symbols, never on fuzzy words.

## What it injects

1. **Diff leg (base — `git` only):** `git diff` of each named file that exists
   in the workspace, capped.
2. **Symbol leg (neuron — FAIL-OPEN):** `code_query explain <symbol>` for each
   backtick symbol — **only if `tools/code_query.py` exists**. On a base-only
   install (no neuron) this leg is silently skipped; the diff leg still runs.
   This mirrors how `dinomem-open-notes` treats the neuron-only `claim_note.sh`:
   present → use it, absent → skip cleanly, never break.

If nothing resolves (named a file that doesn't exist, no diff, no symbol hit) →
**no injection** (empty is not an error).

## Guarantees

- **react-only** — never blocks, cancels, or rewrites a message.
- **never breaks the pipeline** — whole handler is wrapped in try/catch; any
  error just logs and returns.
- **bounded** — ≤3 paths, ≤2 symbols per message; each block capped (~2KB diff /
  1.5KB symbol) so a huge repo can't blow up the turn.
- **8s per-subprocess timeout** — a hung `git`/`python3` can't stall the turn.

## Tuning

- Env `OPENCLAW_WORKSPACE` / `DINOMEM_WORKSPACE` resolve the workspace if the
  event context doesn't carry it.
- To disable, `openclaw hooks disable context-inject`.

## Enable

Scaffolded into `<workspace>/hooks/context-inject/`. Enable + restart:
```
openclaw hooks enable context-inject
```
Verify: `tools/route.py verify hook context-inject`.
