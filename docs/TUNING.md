# Tuning guide (manual, strongly recommended)

Not patched automatically — skipping these hurts cost, performance, response speed, and memory quality. Set based on your model.

---

## Compaction tuning

**`reserveTokens`** — **leave it at the default `20000` unless you have measured a reason not to.**

> ⚠️ **Earlier versions of this guide said to set `contextWindow - 200000` (e.g. `800000` on a 1M model). Do not do that.** It silently breaks compaction. The reason is that `reserveTokens` is **global and absolute**, but it is applied against **whichever model is currently running — including your `compaction.model`**, which is usually a *different, smaller* model. There is no per-model form of this knob.

What a 1M-sized reserve actually does, measured on both OpenClaw generations:

| OpenClaw | `reserveTokens` | primary (1M window) | compaction model (131k window) |
| --- | --- | --- | --- |
| `< 2026.9.3` | `800000` | 200,000 prompt budget ✅ | **8,000** ❌ compaction can never run |
| `< 2026.9.3` | `20000` (default) | 980,000 | 111,072 ✅ |
| `>= 2026.9.3` | `800000` | 750,000 (**not** the 200k you asked for) | 98,304 ✅ |
| `>= 2026.9.3` | `20000` (default) | 980,000 | 111,072 ✅ |

Two separate failure modes, one per generation:

- **Before `2026.9.3`**, an absolute reserve larger than a model's window is clamped to a flat **8,000-token** prompt budget (`MIN_PROMPT_BUDGET_TOKENS`). Any compaction model smaller than your reserve is then permanently stuck: every attempt returns `context_overflow` instead of compacting, and the session keeps growing. Measured live: a session reached ~485k tokens with every provider-side compaction failing on `timeout` / `context_overflow`.
- **From `2026.9.3` onward**, `resolveEffectiveCompactionReserveTokens()` caps the reserve at **25% of the resolved model window** (`MAX_COMPACTION_RESERVE_RATIO = 0.25`), so the 8k collapse is gone — but your big number is silently cut down and the "keep active context at 200k" intent **is not achieved anyway**. Asking for `800000` on a 1M model yields a 250k reserve, not 800k.

So the old formula is harmful on old versions and inert on new ones. The default is correct on both.

**If you specifically want a smaller active context** (for speed or summary quality), do **not** express it as a huge reserve. On `>= 2026.9.3` the ceiling is 25% of the window no matter what you type; pick your number *under* that cap and verify it against your **compaction model's** window, not your primary's.

**`keepRecentTokens`** — set to 10% of `min(contextWindow, 200000)` (so `20000` for most setups). Minimum tokens preserved from the most recent window during compaction — protects immediate context continuity.

**`reserveTokensFloor`** — keep this equal to `reserveTokens` (default `20000`). It is a *floor*, so leaving it high while lowering `reserveTokens` silently re-raises the reserve and reintroduces the collapse above.

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

**Tested models (mine).** For the **compaction model**, I tested **Gemini Flash 3.8** and it works very well. For the **primary model**, I tested **Sonnet 5 and Opus 5** — both work very well.

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
