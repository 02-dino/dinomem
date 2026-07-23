# Tuning guide (manual, strongly recommended)

Not patched automatically — skipping these hurts cost, performance, response speed, and memory quality. Set based on your model.

---

## Compaction tuning

**`reserveTokens`** — set to `contextWindow - 200000`. If your model is `200000` or under, set it to `20000` instead. Keeps active context below 200k, which fixes three things: context bloat, response speed (inference slows non-linearly above 200k), and memory quality (leaner sessions = better compaction summaries).

**`keepRecentTokens`** — set to 10% of `min(contextWindow, 200000)`. Minimum tokens preserved from the most recent window during compaction — protects immediate context continuity.

**`model`** — compaction (summarizing session context) is a **no-reasoning bulk task**, the same tier as dinomem's `extract_memory` / `memory_review`. Set `agents.defaults.compaction.model` to your cheap, high-context model — this is now **the single anchor**: dinomem's [`DINOMEM_CHEAP_MODEL`](#model-selection) resolution auto-follows whatever you set here, so one change here routes every non-reasoning dinomem script too, no separate export needed. If unset, OpenClaw uses your default model for compaction too (works, just costs more). dinomem does not set this for you — you (or your install agent) pick it, since the right model depends entirely on what you have.

**`memoryFlush.model`** — the silent memory-flush turn (reads the session tail, writes the bare daily `memory/YYYY-MM-DD.md` that feeds `startupContext`) is the **same no-reasoning bulk tier** as compaction. By default it runs on whatever your **live session model** is — so on a reasoning-heavy default (e.g. an Opus/Pro tier) every flush burns your most expensive model on a write-to-disk chore. Set `agents.defaults.compaction.memoryFlush.model` to the **same cheap, high-context model** as `compaction.model` (and `DINOMEM_CHEAP_MODEL`). The override is exact — it does **not** inherit the session fallback chain. Caveat: the flush turn decides what's worth keeping; a cheap model is fine for extract-and-write, but if flushed notes ever look thin, bump it up a tier.

Set these under `agents.defaults.compaction` in `openclaw.json`. See `references/openclaw-config-snippet.json5` for annotated examples.

---

## Model selection

**Base dinomem is all no-reasoning bulk work.** `extract_memory` and
`memory_review` are high-volume text ops (extraction, summarization) — the same
tier as OpenClaw compaction. None of base dinomem's own scripts need a reasoning
model.

| Tier | Scripts | Recommended model | Why |
|------|---------|-------------------|-----|
| No-reasoning (bulk) | `extract_memory`, `memory_review`, **+ OpenClaw compaction & memoryFlush** | Cheapest model with the **highest context window** you have | High-volume text ops (extraction, summarization, context compaction, daily-file flush). Context window matters more than reasoning depth. Set `compaction.model` (the anchor) + `compaction.memoryFlush.model`; `DINOMEM_CHEAP_MODEL` auto-follows `compaction.model`, so you don't set it separately (only export it to *override* the anchor for a specific script). |

**Tested models (mine).** For the **non-reasoning** tier, I tested **Haiku 4.5** and it works very well. For the **reasoning** tier (neuron), I tested **Opus 4.8 and Kimi K3** — both work very well.

**Default behavior:** every script uses your OpenClaw default model (`agents.defaults.model.primary`). Nothing to configure.

**To cut cost (opt-in):** set `DINOMEM_CHEAP_MODEL` to your cheapest high-context model. All of base dinomem's scripts are no-reasoning, so they all honor it.

```bash
# example: route bulk scripts to a cheap 1M-context model
export DINOMEM_CHEAP_MODEL="your-provider/your-cheap-high-context-model"
```

- Unset = no change (default-safe). One model runs everything, exactly as before.
- dinomem never auto-swaps models. It only routes when you set the var.

> **Reasoning tier (only if you add [dinomem-neuron](https://github.com/02-dino/dinomem#want-more--dinomem-neuron-private-repo)):**
> neuron adds scripts that *do* need a reasoning model — `memory_synthesis` and
> `contradiction_check` (emergent insight, contradiction logic). Those always run
> on your **default** model and ignore `DINOMEM_CHEAP_MODEL` on purpose,
> additionally requesting thinking via `DINOMEM_REASONING_THINKING` (default
> `high`). neuron's L4 (`memory_promote`, `generate_topic_index`) is non-reasoning
> and honors `DINOMEM_CHEAP_MODEL` like base. If you only have base dinomem, this
> tier doesn't apply — you have no reasoning scripts to route.

**Provider requirements:** scripts call the LLM through the OpenClaw gateway, so they use whatever providers you already have configured — no extra account needed. If the gateway is ever unreachable, dinomem falls back to a direct API call using your **own default model's provider**. **OpenRouter is optional** — it is only used as a fallback target if it happens to be the provider you have. A gateway-only setup with no direct-API key still works (the fallback is simply skipped).

**For autonomous installers (AI agents):** this is opt-in and default-safe — skip it and everything works. If you are cost-optimizing, read the user's `openclaw.json` model catalog, pick the cheapest model whose context window is large enough for `extract_memory` batches (~80k input), and set the two no-reasoning config levers to it: `agents.defaults.compaction.model = <that model>` (the anchor — `DINOMEM_CHEAP_MODEL` auto-follows this, so no separate export is needed) **and** `agents.defaults.compaction.memoryFlush.model = <that model>` (a distinct config path that does *not* auto-follow; OpenClaw compaction and the memoryFlush daily-file writer are the same bulk tier). Do not pick a low-context model — it will truncate extraction batches. Leave `model.primary` on the default (and, if dinomem-neuron is also installed, its reasoning scripts stay on the default automatically). If you cannot confidently rank the catalog, leave everything unset (default-safe) or recommend the choice to your human instead of guessing.
