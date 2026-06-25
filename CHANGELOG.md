# Changelog

## 1.2.6

### Docs
- **`compaction.memoryFlush.model` documented as a cost lever.** The memory-flush
  turn (reads the session tail, writes the bare daily `memory/YYYY-MM-DD.md` that
  feeds `startupContext`) is the same no-reasoning bulk tier as compaction, but it
  ran on the **live session model** by default — so a reasoning-heavy default
  (Opus/Pro) burned the most expensive model on a write-to-disk chore every flush.
  README and `references/openclaw-config-snippet.json5` now show pinning
  `agents.defaults.compaction.memoryFlush.model` to the **same** cheap,
  high-context model as `compaction.model` / `DINOMEM_CHEAP_MODEL`. Override is
  exact (does not inherit the session fallback chain). Default-safe: unset = prior
  behavior. Documented in the Compaction tuning section (alongside its sibling
  manual lever `compaction.model`), the Model-selection table, and the
  autonomous-installer note — treated as a third same-tier lever alongside
  `compaction.model` and `DINOMEM_CHEAP_MODEL`. Kept out of the auto-patch config
  table since, like `compaction.model`, it is manual/opt-in (the table lists only
  what install.sh writes).

## 1.2.5

### Changed
- **Base post-install hint sets expectation that neuron re-surfaces this later.**
  Added one line so a base-only installer knows the model-selection picture
  completes at neuron install (when the reasoning tier appears) — they don't need
  to remember it now. Pairs with neuron 1.2.2, which repeats the recommendation
  at the moment it becomes relevant. Fixes the timing gap where the advice landed
  too early at base install and was never repeated.

## 1.2.4

### Docs
- **Model-selection docs no longer imply base dinomem has reasoning scripts.**
  The previous table listed the reasoning tier (`memory_synthesis`,
  `contradiction_check`, `memory_promote`) inline — but those ship with
  **dinomem-neuron**, not base. Since most people install base first (often
  without neuron), that was confusing. Now: the table shows only base's actual
  no-reasoning scripts; the reasoning tier is moved to a clearly-labeled
  “only if you add neuron” callout. The post-install hint and “For autonomous
  installers” note were reworded the same way — base dinomem is all no-reasoning;
  reasoning routing is neuron-only and dormant until neuron is installed.

## 1.2.3

### Changed
- **Post-install `MODEL_HINT` is now agent-actionable and covers compaction.**
  Extended the install.sh hint so an autonomous installer parsing stdout sees the
  full same-tier picture in one block: set **both** `DINOMEM_CHEAP_MODEL` **and**
  `agents.defaults.compaction.model` to the same cheap high-context model (read
  the user's `openclaw.json` catalog, pick one with enough context, or recommend
  to the human). Reasoning scripts + `model.primary` stay on the default; never
  auto-pick a low-context model; leave unset if unsure (default-safe). README
  “For autonomous installers” note updated to match — so the v1.2.2 compaction
  guidance is reachable by a headless agent, not just a human reading the README.

## 1.2.2

### Docs
- **Compaction model guidance aligned with `DINOMEM_CHEAP_MODEL`.** OpenClaw's
  compaction (summarizing session context) is a no-reasoning bulk task — the same
  tier as `extract_memory` / `memory_review`. README “Compaction tuning” now
  recommends setting `agents.defaults.compaction.model` to the **same** cheap,
  high-context model used for `DINOMEM_CHEAP_MODEL`; the “Model selection” table
  lists compaction in the no-reasoning tier; and `openclaw-config-snippet.json5`
  carries a commented `model:` example. Documentation only — dinomem does **not**
  auto-set `compaction.model` (no model auto-guessing, no clobbering OpenClaw
  config). The right model depends on what the user has.

## 1.2.1

### Fixed
- **Direct-API fallback no longer assumes OpenRouter.** When the OpenClaw gateway
  is unreachable, `call_llm` now falls back to the user's **own default model on
  its native provider** (Anthropic, Kimi, Gemini, xAI, ninerouter, OpenRouter,
  etc.), instead of a hardcoded OpenRouter endpoint. OpenRouter is now optional —
  used only if that is the provider the user actually has. If nothing is
  resolvable, the fallback is skipped gracefully (gateway-only setup) with no
  crash.
- **Provider resolution is now prefix-aware.** `get_api_key_from_openclaw`,
  `get_api_base_from_model`, and `get_api_format_from_model` previously treated
  any `provider/model` id as OpenRouter. They now resolve the real provider from
  the leading routing segment (e.g. `ninerouter/`), falling back to OpenRouter
  only when that provider has no key.
- **Direct fallback request hardening.** Strips the routing-provider prefix from
  the model id for OpenAI-compatible proxies, sends `stream: false`, and the
  response parser now reassembles SSE (`data:` chunk) responses from proxies that
  stream regardless. Uses the provider's real API format instead of hardcoded
  `openai`.

## 1.2.0

### Added
- **`DINOMEM_CHEAP_MODEL` cost lever (opt-in).** No-reasoning bulk scripts
  (`extract_memory`, `memory_review`) route to this model when set; reasoning
  scripts always use the OpenClaw default. Unset = no change (default-safe).
  Implemented in `call_llm`: passes `--model` to the gateway for no-reasoning
  calls when the var is set, and falls through to it on the OpenRouter fallback.
- **`DINOMEM_REASONING_THINKING` (default `high`).** Reasoning calls now pass a
  `--thinking` level to the gateway, so `reasoning=True` actually engages the
  model's thinking budget (previously the flag only affected the unused fallback
  field).
- **README “Model selection” section** (under Compaction tuning): two-tier table,
  the env var, and a “For autonomous installers” note (opt-in, default-safe,
  agent-actionable).
- **Post-install `MODEL_HINT:` line** — machine-greppable, dual human/agent.

### Fixed
- **Gateway stdout JSON hardening.** `call_llm` now slices from the first `{`
  before `json.loads`, so non-JSON noise prepended to stdout (e.g.
  `[state-migrations]` warnings) no longer corrupts a successful gateway response
  and trigger a false OpenRouter fallback.

## 1.1.4

### Changed
- README “Why dinomem is different”: added a closing thesis paragraph — because
  extraction/dedup/review are done by an LLM (not a fixed embedding algorithm),
  dinomem’s judgment compounds with model quality and gets sharper every time the
  underlying model improves, with no retraining or rewrite. Embedding-bottlenecked
  systems stay flat; dinomem rides the model-capability curve.

## 1.1.3

### Changed
- **README repositioned.** New title “The Memory Layer That Gets Sharper Over Time”
  (cool/memorable, pain-contrast vs. systems that bloat) replacing the generic
  “Dino Agent Memory”. SEO-loaded subtitle merges the keyword phrase (self-curating
  long-term memory for AI agents) with the noise/bloat contrast and the concrete
  differentiator (distill / dedup / recall-before-act).
- Brand language aligned across products: dinotrust = self-enforcing,
  dinomem = self-curating.

## 1.1.2

### Changed
- README: added an explicit warning in the "Memory pinning" section that `memory/`
  is cron-managed. Only `_`-prefixed files (e.g. `_pin_*.md`) are protected from
  cleanup; hand-dropped untagged `.md` files may be deduped, TTL-expired, bootcheck-
  removed, or daily-flush pruned. Documents the per-pattern retention rules
  (`_pin_`, `_note_`, bare `YYYY-MM-DD.md`, extraction files, `MEMORY.md`).

## 1.1.1

### Fixed
- **Gateway crash on install (invalid config key).** `install.sh` wrote
  `agents.defaults.workspaceBootstrap = "always"`, but `workspaceBootstrap` is not
  a valid OpenClaw config key (it does not exist in the schema on any version).
  Because `agents.defaults` is `additionalProperties: false`, the unknown key made
  the gateway reject `openclaw.json` and crash on load (reported on OpenClaw 2026.6.1).
  - The valid key is `contextInjection`. `install.sh` now sets
    `agents.defaults.contextInjection = "always"` (already the OpenClaw default;
    set explicitly to document intent) and strips any legacy `workspaceBootstrap`
    key left by older installs so the config validates.
  - `uninstall.sh` now reverts `contextInjection` and also removes the legacy
    `workspaceBootstrap` key if present.
  - Affected users can self-fix without reinstalling: delete `workspaceBootstrap`
    from `agents.defaults` in `~/.openclaw/openclaw.json` and restart the gateway.

### Changed
- README: Prerequisites now state the minimum OpenClaw version (>= 2026.1.0) for the
  `memorySearch` / `compaction` / `contextInjection` config keys. Config-patch table
  row updated from `workspaceBootstrap` to `contextInjection` with the crash rationale.

## 1.1.0

### Added
- **startupContext + guarded daily flush** (enabled by default). On `/new` and
  `/reset`, the last 2 days of raw daily memory are injected on top of the
  always-injected `MEMORY.md` index. `memory_search` pull still handles deep recall.
  - `install.sh` enables OpenClaw `memoryFlush` with a guard prompt that confines
    it to writing the bare daily file `memory/YYYY-MM-DD.md` and forbids editing
    `MEMORY.md` (which dinomem auto-generates and would overwrite).
  - `install.sh` enables `startupContext` (`dailyMemoryDays: 2`).
  - New `procedures/cleanup_startup_daily.py` + cron (02:05 UTC) prunes bare
    `YYYY-MM-DD.md` files older than 2 days. It never touches per-item files,
    `_pin_*`, `_note_*`, or `MEMORY.md`.

### Fixed
- `memory_cleanup.py` now skips bare `YYYY-MM-DD.md` files in the dedup, TTL, and
  bootcheck passes. Previously the bootcheck pass could delete an untagged flush
  file that happened to mention a framework keyword.

### Changed
- README, `references/architecture.md`, and config-patch tables updated: the
  `memoryFlush`/`startupContext` rows now reflect the enabled-by-default behavior
  and document the underscore-vs-hyphen filename matcher and the guard rationale.

### Upgrading
Existing installs: run `bash scripts/update.sh` (pulls latest, re-runs the
installer with `--force`). This re-patches `openclaw.json` (memoryFlush prompt +
startupContext), installs `cleanup_startup_daily.py`, and registers its cron.
Memory data and logs are preserved.

## 1.0.0

- Initial release.
