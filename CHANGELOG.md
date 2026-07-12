# Changelog

## Unreleased

Multilingual embeddings by default. **Recommended for anyone whose notes/queries aren't purely English.** The local TEI embedding server now ships `intfloat/multilingual-e5-small` instead of the English-centric `all-MiniLM-L6-v2` — same 384-dim vectors and comparable footprint (~0.45 GB, 118M params, XLM-RoBERTa), but strong retrieval across 100+ languages (English, Indonesian, Arabic, Chinese, French, Italian, Russian, Spanish, …) and double the context (512 vs 256 tokens).

### Fixed
- **Hook liveness self-check with global-dir self-heal (silent hook no-op fix).** `openclaw hooks enable <name>` only sets `hooks.internal.entries.<name>.enabled=true`; it does **not** verify the gateway can actually *load* the hook. If the installer's `$WS` is not the gateway's scanned workspace, the hook copied into `$WS/hooks/` sits in an unscanned dir and **silently never fires** — while the installer still reports success. install.sh now runs a post-install check that parses `openclaw hooks check --json` and asserts each dinomem hook (`dinomem-reset-extract`, `dinomem-open-notes`) is in `eligible[]`; if not, it copies the hook pack into the always-scanned global `~/.openclaw/hooks/<name>/` and re-enables, warning the user to restart. Uses `--json` (not a `hooks list` grep, which gives false negatives — the human table emoji-wraps and splits the hook name across lines). Converts a previously-invisible failure into a visible, self-healing one.
- **Cron upsert no longer errors `Choose exactly one schedule`.** The install-time cron *edit* path (refreshing an existing job's message + re-enabling it) was re-passing `--cron <expr>` to `openclaw cron edit`, which the OpenClaw cron CLI (2026.6.x) rejects on an already-scheduled job (`Error: Choose exactly one schedule: --at, --every, or --cron`). Non-fatal (install still exited 0, jobs registered via the add path) but noisy. `cron edit` now refreshes message + `--enable` only and preserves the existing schedule as-is (both duplicate registration blocks fixed).

### Changed
- **Default embedding model → `intfloat/multilingual-e5-small`.** `docker/docker-compose.tei.yml` and `scripts/install.sh` (docker-run fallback + the `openclaw.json` `memorySearch` / `tei-embed` provider patch) now provision e5-small. TEI image bumped **cpu-1.5 → cpu-1.6** (cpu-1.5 fails to download this model — a known image bug; cpu-1.6 serves it cleanly). Added `--max-input-length 512` to use e5's full context. `references/openclaw-config-snippet.json5`, `README.md`, and `references/architecture.md` updated to match.
- **New/first installs get multilingual retrieval out of the box; no per-user rebuild** (fresh installs embed against e5 from the start). Existing installs that were already using all-MiniLM would need to re-embed to benefit — not automated here. To pin the old model, set `--model-id sentence-transformers/all-MiniLM-L6-v2` in the compose/run command.
- **Fix: removed `--max-input-length` flag** (added in an earlier draft of this change, never released) — TEI 1.6.1's CLI does not accept it (`error: unexpected argument '--max-input-length' found`, confirmed against a live container). Not needed: TEI derives the model's real max length (512 for e5-small) from the model config automatically — confirmed via `/info` on a running cpu-1.6 container serving e5-small.

### Added
- **`dinomem-open-notes` internal hook (`hooks/dinomem-open-notes/`) — deterministic open-work injection at bootstrap.** Fires on `agent:bootstrap` and injects a short, blocking manifest of your **open** notes (`memory/_note_*.md` with `status: in_progress` or `pending`) directly into the model's context every session — path · title · status · one-line `done_when`, sorted most-recent-first and capped (`DINOMEM_OPEN_NOTES_MAX`, default 5; overflow shown as `+N more`). This **replaces** the old AGENTS.md `M_session_start` rule ("glob `memory/_note_*.md` and read the open ones before answering"), which was a plea the model could silently skip — the docs flagged that skip as the single most common silent failure. Now the glob happens deterministically in the hook, so the open-work list is always present. Pointer manifest (near-zero tokens), rendered as a must-read-before-answering directive so the follow-up `read` is cheap and self-triggering via `done_when` relevance. Zero-op on a clean workspace; never blocks bootstrap (errors are logged and swallowed). Injected under the `AGENTS.md` bootstrap name so it survives the subagent/cron session-file allowlist on main interactive sessions.
- **`install.sh` section 2c/2d**: copies `hooks/dinomem-open-notes/` into `<workspace>/hooks/` + runs `openclaw hooks enable dinomem-open-notes`, and copies the new `skills/*` into `<workspace>/skills/`. Idempotent (skips if present; `--force` overwrites).
- **Three skills (`skills/memory-pinning`, `skills/backup-restore`, `skills/self-config`).** The verbose `memory_pin`, `backup_restore`, and `self_config` blocks were lifted out of the always-injected AGENTS.md block into on-demand skills, leaving a 1-line `when_to_use` stub each. The injected AGENTS.md block shrinks from ~66 to ~40 lines — the recall-discipline rules (M0–M3, `investigate_before_act`) stay inline (a skill loads only when the model chooses to, which can't back-stop always-on recall discipline), while the reference material (pin/note/project schema, backup commands, config routing) moves to skills the model pulls when relevant.
- **`uninstall.sh`**: under `--purge`, removes `hooks/dinomem-open-notes/` (+ disables the hook) and the three `skills/*` directories.
- **Upgrade-safe LLM crons (`Daily Note Review`, `Pending Note Reminder`) — prompt refresh on re-run, no freeze, no duplicates.** The installer previously did "if a cron named X exists → skip it," so upgrading an older install left both OpenClaw `agentTurn` crons running their *old* prompts forever. Registration is now an **upsert**: if the job exists, its prompt is refreshed in place via `cron edit <id> --message` (and its schedule via `--cron` if changed) while staying **enabled** on its own schedule; if it does not exist, it is created. The `--dry-run` path reports register/refresh accordingly. Re-running the installer is idempotent. The OS-crontab jobs were already upgrade-safe via the `upsert_cron` helper; this closes the gap for the two LLM crons.
- **`scripts/doctor.sh` — TEI embed-server health check.** Curls the TEI server (`/health` + a real embed round-trip), reports OK/unhealthy with basic model info, and prints actionable hints (container not running, wrong image tag, model still downloading). Wired into `install.sh` so a fresh install self-verifies the embedding server the memory pipeline depends on.

### Docs
- **TEI troubleshooting (FAQ.md):** the `cpu-1.5` "relative URL without a base" model-download bug (use `cpu-1.6`), and the `--max-input-length` crash-loop on TEI 1.6.1 (flag removed; TEI derives max length from the model config — 512 for e5-small).
- **Protected-config CLI-writer workaround (FAQ.md):** some `openclaw.json` paths (e.g. `models.providers.*.models`, `agents.defaults.memorySearch.model`) are protected from the gateway `config.patch`/`config.apply` RPC; use the `openclaw config set ... --strict-json --replace` CLI writer instead of a raw file edit.
- **Symmetric-embed intent** documented inline in `extract_memory.py` / `memory_cleanup.py` / `memory_review.py`: these raw-embed procedures are intentionally unprefixed/symmetric (dedup/similarity, not asymmetric query→doc retrieval), with a pointer to the `DINOMEM_EMBED_PREFIX` convention used at retrieval callsites.

## 1.2.12

0-delay memory extraction on manual `/new` / `/reset` via an internal hook. **Recommended for all users** — closes the residual ≤15-min post-reset memory-blindness window introduced in 1.2.11.

### Added
- **`dinomem-reset-extract` internal hook** (`hooks/dinomem-reset-extract/`). Fires on `command:new` and `command:reset` and immediately launches `procedures/auto_session_reset.py` in the background — the same pipeline the `*/15` cron runs (adopt core reset-archives → `extract_memory.py` → optional `session_ingest.py`). Result: the session you just left is mined for memory at the instant you `/new`, not up to 15 minutes later. The handler is fire-and-forget (detached child process) so `/new` / `/reset` acknowledgements are never delayed. Fully idempotent: the pipeline holds `/tmp/dinomem_auto_reset.lock` and deduplicates per-archive (processed-log) and per-content (ingest hash), so a concurrent cron tick is harmless. If the hook races core's archive rename and misses the file, the regular cron catches it next tick — strictly an improvement, never a regression.
- **`install.sh` section 2b**: copies `hooks/dinomem-reset-extract/` into `<workspace>/hooks/` and runs `openclaw hooks enable dinomem-reset-extract` automatically. Idempotent (skips if already present; `--force` overwrites).
- **`uninstall.sh`**: removes `hooks/dinomem-reset-extract/` and disables the hook on uninstall.

### Upgrade
`git pull` then re-run `scripts/install.sh --force` to deploy the hook and enable it. Requires OpenClaw gateway restart after enable for the hook to activate.

## 1.2.11

Manual `/new` `/reset` sessions were never mined for memory. **Recommended for all users** — recovers memory that was silently being lost on every manual reset.

### Fixed
- **Silent permanent memory loss on manual `/new` / `/reset`.** OpenClaw core archives a session the instant it is manually reset by renaming `<session>.jsonl` → `<session>.jsonl.reset.<ISOms>Z` in real time. But the memory pipeline never saw these: `extract_memory.py` only globs `*.archived.*.jsonl`, and `session_reset.py`'s `get_orphaned_files()` explicitly **skips** any name containing `.reset.`. The two archive namespaces were disjoint — core's real-time `.jsonl.reset.<iso>Z` files and the pipeline-visible `.archived.*.jsonl` files never met. Result: **any session you explicitly `/new` or `/reset` had its transcript archived by core but never extracted into `memory/` — permanent loss.** (On one live install this had silently stranded ~1 month / 54 sessions of un-mined transcripts, oldest dating to the first manual reset.) Age-out orphan cleanup masked it partially: sessions that aged into the orphan sweep before you manually reset them were caught by that path; only explicitly-reset sessions fell through.
  - **Fix:** new `adopt_core_reset_archives()` step (Step 1.5 in `auto_session_reset.py`'s reset stage, after orphan cleanup, before archive pruning). It globs `*.jsonl.reset.*`, recovers the original session stem, preserves core's reset timestamp, cleans the transcript with the same `cleanup_jsonl_content()` used for orphans, and writes `<session>.archived.reset.<ts>.jsonl` into the pipeline-visible namespace — then removes core's stray file. `extract_memory.py` (and, on neuron, `session_ingest.py`) then pick it up on the same tick. Idempotent (target-exists check + the extract dedup log), so re-runs are no-ops. Doubles as a **one-time backfill**: the first run after upgrade sweeps every stranded historical `.jsonl.reset.<iso>Z` file and recovers its memory.

### Known limitation (follow-up)
- Adoption runs on the `*/15` cron tick, so there is still a **≤15-min lag** between a manual `/new` and its memory being extracted (down from *permanent loss*). Closing the residual window to 0-delay requires a `session_end` hook that fires at reset time; tracked as a separate follow-up.

### Upgrade
`git pull` then re-run `scripts/install.sh --force` to deploy the fixed `procedures/session_reset.py`. First cron run after upgrade backfills any historical manual-reset sessions.

## 1.2.10

MEMORY.md index-bloat fix + neuron-coexistence guard. **Recommended for all users; required before installing neuron.**

### Fixed
- **MEMORY.md bloat from the "Previous Session" line.** `get_previous_session_topics()` built the Previous Session keyword line from the dated-slug path with **no per-item cap** — a heavy work-day (many `memory/YYYY-MM-DD_*.md` files) produced hundreds of slugs joined into one multi-KB line. `trim_memory_index()` removes whole lines and only touches `[TAG]` entries, so that single over-long line survived trimming and bloated the injected MEMORY.md (risking silent truncation on injection). Fix A: cap the slug path (15 items / 600 chars), symmetric with the already-capped word-freq fallback. Fix B: `trim_memory_index()` now also collapses any single over-long line (> 800 chars) in place as a safety net, not just `[TAG]` entries. (Same class of bug fixed in neuron 1.2.5's `generate_topic_index.py`; base carried the identical shape in `memory_cleanup.py`.)

### Changed
- **MEMORY.md writer ownership (neuron coexistence).** When the neuron layer is installed, `generate_topic_index.py` becomes the authoritative MEMORY.md writer. `memory_cleanup.py` now auto-detects neuron (presence of `procedures/generate_topic_index.py`) and **yields** the MEMORY.md-writing steps (recency / open-projects / trim) to it, preventing two writers racing on the same file after a base→neuron upgrade. Dedup / bootcheck / archive-prune still run in `memory_cleanup` either way. Base-only installs are unaffected (neuron absent → normal behavior). Override with `DINOMEM_FORCE_INDEX_WRITER=1`.

## 1.2.9

Memory pipeline reliability fix. **Recommended for all users.**

### Fixed
- **Silent extraction failure on multi-Node hosts.** `extract_memory.py` shells out to the `openclaw` CLI, whose `#!/usr/bin/env node` shebang picks whatever `node` resolves first on PATH. On boxes with multiple Node installs, cron could resolve an outdated Node (< 22.19), causing the CLI to hard-exit and every LLM extraction to fail — writing **0 memory notes** while archives were still marked processed. Extraction now resolves a compatible Node (>= 22.19) at runtime (scans PATH, common install roots, and nvm; verifies version), self-healing across Node upgrades/moves. Emits a loud warning if no valid Node is found instead of failing silently.
- **Permanent memory loss on transient LLM failure.** A failed extraction previously still marked the archive "processed", so it was never retried — dropping that session's memory permanently. Failed LLM calls now return an `LLM_FAILED` sentinel and are **not** marked processed, so they auto-retry on the next run. Genuinely-empty sessions are still marked (correct).
- **False extraction failure from gateway stdout noise.** Tolerant JSON parsing: slice from the first `{` so gateway warning lines prepended to stdout can't trigger a false fallback on an otherwise-successful call.

### Upgrade
`git pull` then re-run `scripts/install.sh --force` to deploy the fixed `procedures/extract_memory.py`. dinomem-neuron users: update the dinomem base — neuron does not ship this file.

## 1.2.8

Addresses install-experience feedback in [#2](https://github.com/02-dino/dinomem/issues/2).

### Fixed
- **`install.sh` non-fatal shell errors during config + AGENTS.md patching**
  (`line NNN: contextInjection: command not found`, `line NNN: content: No such
  file or directory`). Root cause: the `openclaw.json` Python block ran in an
  **unquoted** heredoc (`<<PYEOF`), so backticks in its comments were executed as
  shell command substitution; and the AGENTS.md/TOOLS.md blocks were built as
  **double-quoted strings** whose embedded quotes closed the string early,
  exposing `<content>`/`<slug>` placeholders as shell redirections. The AGENTS.md
  case was not merely cosmetic — in some shells the broken string produced an
  **empty managed block** (policy silently not installed). Fixed by switching the
  config block to a quoted heredoc (`<<'PYEOF'`) with values passed via
  environment, and building the AGENTS.md/TOOLS.md blocks as quoted heredocs
  captured into a variable. Generated content is byte-for-byte unchanged.

### Added
- **`--dry-run` flag for `install.sh`.** Previews every change — directories,
  copied scripts, crontab entries, the OpenClaw Daily Note Review cron, the
  `openclaw.json` patch, and the AGENTS.md/TOOLS.md blocks — **without writing
  anything**. Idempotency-aware: prints `[plan]` for actions a real run would
  take and `[skip]` for what already exists. Exits before any mutation.
- **Config validation on install.** After patching `openclaw.json`, the
  installer now runs `openclaw config validate` and, on failure, **rolls back to
  the exact pre-write bytes** and prints the schema error (which names the
  offending field and path). This turns the original `workspaceBootstrap`
  gateway-crash class into a caught, named, auto-reverted error **before** the
  user restarts — instead of a silent crash at gateway startup. Set
  `DINOMEM_SKIP_CONFIG_VALIDATE=1` to opt out (e.g. when the `openclaw` CLI is
  unavailable). Generalizes the previous single-key `workspaceBootstrap` strip
  into schema-wide validation.

### Not addressed (upstream)
- Suggestion that **gateway crash messages name the offending field**: that is an
  OpenClaw core concern, not dinomem's to fix. The install-time validation above
  is dinomem's mitigation — it catches the bad key before a restart and names the
  field itself.

## 1.2.7

### Changed
- **Dropped the `(any language)` tag from the `memory_pin` trigger.** It was a
  no-op at runtime: the trigger is LLM-judged (the agent interprets intent), so
  multi-language pinning works regardless of the tag — the tag changed no
  behavior, only added an every-turn-injected token. Removing it also fixes a
  doc inconsistency: `_pin` carried the tag, the `_note` (transient) trigger
  didn't, which could wrongly read as "`_note` is English-only." Both triggers
  are now tagless and consistent; language-agnostic behavior is unchanged (still
  intent-judged, not keyword-matched).

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
