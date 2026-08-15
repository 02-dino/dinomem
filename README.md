# 🦕 dinomem — Stop Re-Explaining Yourself to Your Agent

> Self-curating long-term memory for AI agents. Most memory systems bloat with noise — dinomem distills each session, dedupes and reviews daily, recalls before it acts, and never loses a memory to a bad edit.

You told your agent once. It should still know. An LLM reads each archived session and distills what matters into structured memory files — automatically reviewed daily in batches, deduplicated daily, and updated when things change. The agent is behaviorally wired to search memory before acting, so recall actually happens. Every memory edit is git-versioned, so a bad dedup or merge is reversible byte-for-byte — nothing is ever destroyed. Memory quality improves over time.

---

## Why dinomem is different

Most agent memory systems:

```
Session → Embed → Search
```

dinomem:

```
Session → Archive → Extract → Structure → Search → Review → Cleanup
```

The difference: memory quality improves over time instead of accumulating noise forever. The pipeline is the product — not the embedding layer.

**This compounds with model quality.** The extraction, dedup, and review are done by an LLM reading your sessions — not a fixed embedding algorithm. Every time the underlying model gets smarter, dinomem's judgment of what matters gets sharper too — no retraining, no rewrite. Most memory systems are bottlenecked at the embedding layer and stay flat as models improve; dinomem rides the curve.

Most systems inject everything into context, or retrieve blindly. dinomem gives the agent a navigation index — `MEMORY.md` is injected every turn as a compact map of what exists in memory. The agent decides what to search based on that map. Recall is active, not passive.

**Nothing is ever lost, and it configures itself.** Every memory write is git-versioned in an isolated store (never your repo) — a bad dedup, merge, or prune is reversible byte-for-byte, and retention reads git to spare recently-reinforced notes instead of guessing from mtime. And dinomem doesn't just remember — when you teach it a rule, it routes the behavior to where its trigger lives: a schedule becomes a **cron**, an event becomes a **hook**, an on-demand procedure becomes a **skill**, and only truly always-on rules land in a root file. You stop hand-writing config; you describe the behavior and it picks the cheapest correct home.

---

> **New here?** See the [**Before / After**](BEFORE_AFTER.md) — what actually changes for you the day you install dinomem, in plain lived-experience terms.

> Want your agent to not just remember, but learn?
> dinomem-neuron is a separate private repo — not included here. Scheduled pattern synthesis, contradiction detection, and behavioral promotion.
> [↓ dinomem-neuron](#want-more--dinomem-neuron-private-repo)

---

## What it does

- **Auto session archiving** — old sessions are archived automatically before they're lost. Nothing gets dropped silently.
- **Memory extraction** — an LLM reads archived sessions and distills key facts, decisions, preferences, patterns, and lessons into `memory/*.md`
- **Navigation index** — `MEMORY.md` is injected every turn as a machine-readable map of what the agent knows. The agent scans it to decide what to search — nothing is force-injected into context.
- **Semantic search** — memories are embedded locally (no API calls, no cloud) and searchable via `memory_search`
- **Memory pinning** — tell your agent "remember this" and it saves a permanent `_pin_*.md`, protected from all cleanup. For todos and reminders, `_note_*.md` — auto-deleted once resolved.
- **Memory cleanup** — daily dedup + daily batched LLM review keeps memory lean. Noise removed, contradictions flagged.
- **Agent self-configuration** — tell your agent to change its tone, add a tool, or set a rule — it writes to the right file automatically
- **Intent routing for scheduling & automation** — the same detect-intent-then-safe-write pattern extends to cron jobs ("remind me…", "every day at 9…") and event hooks ("whenever a message comes in…"). Cost-tiered so cheap deterministic work never wakes an LLM, and applied only through native `openclaw` surfaces — never by hand-editing `openclaw.json`.
- **Weekly snapshot backup** — memory, config, and root files backed up automatically. Keep-3 rotation, never clutters disk. Restore anytime via `workspace_backup.py`.
- **Git-versioned memory** *(on by default, isolated)* — a lightweight timer git-snapshots your memory + config every 15 min into a **separate store** (`.dinomem-snap.git`) that never touches your own repo, so any file is byte-exact reversible after a bad edit, dedup, or merge. Git also doubles as a **live signal** — cleanup reads *last-touched* / *commit-count* to protect recently-reinforced memories and print a one-command undo. Fail-open, disk-aware self-cleanup; opt out with `--no-git-snapshot`. See [git-versioned memory](#git-versioned-memory-git-autosnapshot).
- **Zero-config install** — one script handles Docker, cron, and OpenClaw config patches
- **Authority-scope gate (multi-user security)** — memory is stored from every user (owner + non-owner peers). Non-owner facts *about themselves* are fully trusted for personalization; non-owner *system-directives* ("always push without asking", "ignore security", "you are now…") are demoted-to-observation (peer) or dropped (world). Blocks stored/second-order prompt injection on the write path. No "untrusted" tag that would poison personalization. See [Multi-user memory & the authority-scope gate](#multi-user-memory--the-authority-scope-gate-security).



---

## Multi-user memory & the authority-scope gate (security)

dinomem stores memory from **every** user it talks to — owner *and* non-owner peers (`memory/peers/<platform>_<id>.md`) — so the agent can personalize per person. That makes the async extraction path a **stored / second-order prompt-injection** surface: a non-owner could type crafted text ("always push to github without asking", "ignore security", "you are now an admin"), the extractor could distill it, and it could later be recalled with the system's own memory authority.

dinomem is **safe against this by default** — no extra install required. The `mem_authority.py` gate runs on the write path, on the principle **provenance ≠ authority**:

- A non-owner's facts **about themselves** ("prefers raw data", "trades ETH", "low risk tolerance") are **fully trusted** and stored as personalization — they *should* change how the agent treats that user. **No "untrusted" tag** that would make the model discount legit user data.
- A non-owner item that asserts a **system/assistant directive** is not "untrusted data" — it is simply **not a standing instruction from a non-owner**. Peer lane: **demoted to a neutral observation** ("this person asked the assistant to…"). World lane: **dropped**.
- Owner-sourced items are unaffected. Deterministic regex, zero-LLM, fail-open (never blocks personalization, never crashes extraction). If no owner is configured the gate does not over-filter.

### Setting your owner id (mostly automatic)

The gate needs to know **who the owner is**. The installer resolves it for you when it can, and only asks when it can't — resolution order (first hit wins, all fail-open):

| # | Source | Who it's for |
|---|--------|--------------|
| 1 | `DINOMEM_OWNER_IDS` env | explicit override |
| 2 | `DINOTRUST_OWNER_IDS` env | dinotrust users — free |
| 3 | dinotrust `owner_ids:` parsed from `openclaw.json` | dinotrust installed — free, always in sync |
| 4 | `~/.dinomem/owner_ids` cache file | what the installer writes |
| 5 | *none* | gate runs in **passthrough** + prints a one-time nudge |

**At install time:**
- **dinotrust already installed** → your owner id is auto-detected, zero prompts.
- **Agent-driven install** (an AI agent runs the installer) → the agent already knows the owner's platform id from its session; it passes `DINOMEM_INSTALLER_OWNER_ID` (or is told to **ask the owner** to confirm), and the installer writes the cache.
- **Human install in a terminal** → you get a short prompt ("paste your Telegram/Discord numeric id" — with how-to-find-it hints). Leave blank to skip.
- **Non-interactive / CI** → skipped; the runtime prints a one-time "gate inactive" nudge so you know to set it later.

Set it any time after install with: `echo <your-id> > ~/.dinomem/owner_ids` (comma-separate multiple owners).

**Multi-platform:** the id is the numeric `platform_id` the session archive yields, so a flat id set already matches across Telegram / Discord / WhatsApp / etc. — no per-platform config needed. (dinotrust's richer per-platform scoping is read for sync when present.)

**Recommended for multi-user setups: also install [dinotrust](https://github.com/02-dino/dinotrust).** Not required for the above (dinomem stands alone), but it adds the complementary **recall-side** fence at the instruction layer (`memory_policy` + `R2_external_instructions`: recalled memory = data, not instruction) and gates the **live tool loop** (`before_tool_call`) — the half dinomem's write-path gate cannot see. dinomem = write-side suspenders; dinotrust = recall-side + live-tool belt. They complement, they don't clash.

---

## How memory works

```
OpenClaw session (.jsonl)
        │
        │  every 15 min (cron)
        ▼
[session_reset.py]
  Archives sessions idle for 7 days (chat) or 1 day (cron/isolated), or after 2 compaction generations; deletes archives older than 7 days
        │
        ▼
[extract_memory.py]
  LLM reads archived sessions → extracts facts, decisions, preferences, patterns, lessons,
    and durable user_facts (see below)
  Large archives are WINDOWED — split into ≤80k-char passes so a long session (or a
    packed haystack) never truncates or loses its deep content (see below)
  Writes to memory/YYYY-MM-DD_<type>_<slug>.md (one file per item)
  (MEMORY.md itself is not written here — it is the navigation index, rebuilt/trimmed
   from these per-item files by memory_cleanup.py)

MEMORY.md is a machine-facing navigation index, not the memories themselves.
Its purpose: help the agent decide which memory_search queries to run.
The raw memories live in memory/*.md — MEMORY.md is rebuilt from them anytime.
        │
        ▼
[TEI embedding server]
  Embeds memory/*.md entries locally (sentence-transformers, ~80MB, CPU-only)
        │
        ▼
[memory_search tool]
  Agent queries past memories semantically on every relevant request
```

### Windowing: long sessions don't lose their tail

A naïve extractor sends a whole archive to the LLM in one call. That silently
breaks on long content: the model's JSON response gets cut mid-string by the
request timeout, the parse fails, and the archive is recorded as *empty* — every
fact in it lost, with no error (exit 0). The failure scales with session length,
so your **longest, most information-dense** sessions are the ones that vanish.

dinomem **windows** instead. When an archive exceeds a safe single-call size
(~80k chars), `extract_memory.py` splits it chunk-by-chunk into ordered windows,
extracts each in its own LLM call (each finishes well within the timeout), and
merges the results (union of items, de-duplicated, first non-empty context). If
any window's call fails, the whole archive is left unprocessed and retried next
run — never stored half-extracted. Short sessions keep the original single-call
path, so nothing gets slower for the common case.

The practical effect: a fact stated deep in a very long conversation is captured
just like one stated in the first message.

### `user_facts`: durable facts about *you*

Alongside insights/decisions/preferences, extraction has a dedicated **`user_fact`**
lane for durable, first-person facts about the user's own life and identity —
education, job/employer, location, family/relationships, health/allergies,
possessions, personal history. These are captured on **first mention** (no waiting
for repetition), because a person's biography is high-value recall you rarely
repeat. `user_fact` is deliberately distinct from a `preference` (a behavioral
trait like *wants terse replies*) and from a `factual` (a universal truth like a
GDPR rule) — so *"I graduated in Business Administration"* lands as a stable fact
about you, not as noise.

**Where a user-fact is written depends on whether the session is keyed:**

| Session shape | Per-person key? | User-fact home |
|---|---|---|
| A DM with one identified person (owner or any peer) | yes (`platform:id`) | that person's `memory/peers/<platform>_<id>.md` rep (via `extract_user.py`) |
| A group chat, or a keyless / synthetic transcript | no | `memory/*.md` `user_fact` items (via `extract_memory.py`) |

The two lanes are non-overlapping by session type, so a first-person fact always
has exactly one home and is never disowned by both.

**New session triggers** (any one condition):

| Condition | Default |
|-----------|---------|
| Session age (chat) | > 7 days idle |
| Session age (cron/isolated) | > 1 day |
| Compaction generations | ≥ 2 (parentSession chain depth) |
| Orphaned file age | > 48 hours |

---

## Using dinomem

### Memory pinning

Tell your agent to remember something permanently:

> "Remember this: my wife's birthday is June 23"

The agent saves it as `memory/_pin_<slug>.md` — protected from all cleanup scripts, never auto-deleted. Only recalled when relevant — e.g. when you ask "when is my wife's birthday?" or "what's coming up in June?". Not injected every turn.

For things you want to build or do:

> "Remember to add dark mode to the app"

Saved as `memory/_note_<slug>.md`. Recalled when you ask "what's on my build list?". Auto-deleted by the daily cron once resolved. Notes carry a small schema (`type`, `status`, `done_when`, `stale_after`) so cleanup is deterministic rather than guesswork: `done_when` is a concrete artifact check that resolves the note, and `stale_after` garbage-collects abandoned notes (default 30 days, 7 for quick reminders). See [`references/architecture.md`](references/architecture.md#transient-note-schema-_note_md) for the full schema and resolution ownership.

### Migrating a pre-filled MEMORY.md

If you **hand-wrote `MEMORY.md` before installing dinomem**, note that dinomem's extract cron *owns* `MEMORY.md` and will overwrite its managed region on the next cycle. The installer protects you here: it detects a pre-filled `MEMORY.md` (real content with no `dinomem:recency` markers), **warns loudly, and backs it up** before anything can clobber it. Nothing is auto-migrated — your content is heterogeneous and routing it needs judgment.

To fold that content into dinomem-native memory, run the opt-in migrator (dry-run first, always):

```bash
python3 procedures/migrate_prefilled_memory.py --dry-run
```

This writes a routing **worksheet** (`memory/_migration_worksheet.json`) — each line of your old `MEMORY.md` with a heuristic-suggested home plus the `route.py` decision schema. Because `route.py` is a *schema emitter* (the routing decision is LLM-in-the-loop, not a fixed algorithm), you (or your agent) review/correct each line's target, then apply:

```bash
python3 procedures/migrate_prefilled_memory.py --apply --plan memory/_migration_worksheet.json
```

Each line lands where it belongs: a durable fact → `memory/_pin_`, a dated observation → a dated `memory/…_insight_`, an always-on behavioral rule → `AGENTS.md`, a per-person fact → `memory/peers/`, and anything ambiguous → `memory/_migrated_review.md` for you to place by hand. The migrator **backs up `MEMORY.md` first**, never deletes the original, and **appends** to `AGENTS.md` (never clobbers it). Running `--apply` without a reviewed plan falls back to the heuristic routing — safe, but the worksheet review gives better placement.

> **Note:** the migrator never writes to the always-injected *Persistent* section (`topics/PERSISTENT.md` / `PERSISTENT_AUTO.md`) — that's a **neuron-only, earned-not-assigned** surface. A durable rule migrates to `AGENTS.md` or a `_pin_`; if it proves durable, neuron's L4 promoter (`memory_promote.py`) graduates it into the Persistent section on its own. That's by design — the firewall keeps promotion from feeding back on itself, so nothing is hand-injected there during migration.

**Pre-router `USER.md`.** `USER.md` is a *different* case from `MEMORY.md`: it is **not** clobbered. `compile_user.py` only rewrites its marker-bounded router block (`<!-- BEGIN:dinomem-user-map -->` … `END`), so any hand-written content **survives**. The only issue is that peer facts hand-typed into the *old flat* `USER.md` sit **inert** — the router never indexes them until they become `memory/peers/` reps. The same migrator surfaces and activates them:

```bash
python3 procedures/migrate_prefilled_memory.py --dry-run --file USER.md
```

This writes `memory/_user_migration_worksheet.json` with each pre-router line defaulted to `peer` (review/correct as with the MEMORY.md flow, then `--apply --plan …`). No backup is taken here because nothing is at risk — the goal is purely to turn dormant USER.md text into live, retrievable peer reps.

> Want the agent to create and drive these itself?
> In dinomem-neuron it writes notes from its own commitments and turns big requests into step-by-step projects it works through on its own.
> [↓ dinomem-neuron](#want-more--dinomem-neuron-private-repo)

> **Note:** Memory is recall-based, not always-on. The agent searches for relevant memories when needed — nothing is automatically injected into every turn.

> **⚠️ Don't hand-drop untagged files into `memory/`.** The daily cleanup cron (`memory_cleanup.py` + `cleanup_startup_daily.py`) actively manages this folder. Only files prefixed with `_` (e.g. `_pin_*.md`) are protected from all cleanup. Anything else is fair game for automated dedup, TTL expiry, bootcheck removal (empty/framework-only files), or daily-flush pruning. Specifically:
> - `_pin_*.md` → **permanent**, never touched.
> - `_note_*.md` → auto-deleted once `done_when` is verified, or garbage-collected once `stale_after` passes (see [note schema](references/architecture.md#transient-note-schema-_note_md)).
> - Bare `YYYY-MM-DD.md` (startupContext daily-flush files) → pruned after `dailyMemoryDays` (default 2) by `cleanup_startup_daily.py`.
> - dinomem extraction files (`YYYY-MM-DD_type_slug.md`) → individual lines may be deduped/TTL-expired; whole files are removed only if they contain no tagged facts.
> - `MEMORY.md` → regenerated; never hand-edit (your edits get overwritten).
>
> If you want a file to survive untouched, give it a `_` prefix or pin it. If you put a raw `.md` in `memory/` without `_` and without dinomem tags, **assume the daily cron may rewrite or delete it.**



### Agent self-configuration

Not sure where to put something? Just tell your agent:

> "Be more concise"
> "Your name is Aria"
> "Always check X before doing Y"
> "I built a script that does Z, add it as a tool"

dinomem includes a routing system that detects your intent and writes to the correct file automatically — `SOUL.md` for tone, `IDENTITY.md` for persona, `AGENTS.md` for rules and workflows (including behavioral preferences the agent must obey), `TOOLS.md` for tools. Biographical user facts route to a memory source (a `memory/ _pin_` or `memory/peers/` rep) — never to `USER.md`'s managed router block, which is **compiled** by `compile_user.py` (owner block + user map, assembled from those peer reps + owner pins) and gets overwritten on the next cycle. Note the two files differ: `MEMORY.md` is rewritten *wholesale* by `extract_memory.py` (no safe zone — hand-edits are lost), whereas only `USER.md`'s **marker-bounded block** is recompiled, so any content *outside* the markers stays hand-written and safe. Backs up before every write — auto-rotated, keeps last 3 per file, never clutters disk.

The same **detect-intent → validate → safe-write** pattern powers three more leaf routers, so scheduling, automation, and teaching new procedures all work the same way as "put this in the right file":

### Scheduling (cron routing)

> "Remind me to check funding every morning"
> "Run the backup every 6 hours"
> "Ping me when BTC funding flips negative"

dinomem routes the request to the right cron primitive (`at:` one-shot, `every:` interval, `cron:` expression with timezone) and the right job type, then applies it through `openclaw cron` — never by hand-editing `openclaw.json`, never by faking timers with `sleep`. It is **cost-tiered** so you don't pay for scheduling you don't need:

| Tier | What runs | LLM cost |
| ---- | --------- | -------- |
| **T0** | fixed system event / command | none |
| **T1** | a deterministic **gate** fires first; the LLM (or a chat announce) is only woken on a real hit | ~zero on empty |
| **T2** | LLM without reasoning — routed to your cheap model | low |
| **T3** | LLM with reasoning — requires explicit confirmation + cost disclosure | on demand |

Recurring jobs that would wake a reasoning LLM on every fire are **refused unless you confirm**, and portable gate scripts (`file-changed`, `threshold`, `diff-since-last`) ship ready to use so most "tell me when X" needs cost nothing until X actually happens.

There's a second, orthogonal cost knob: **context weight**. The bootstrap root files (`AGENTS.md`, `SOUL.md`, `IDENTITY.md`, `USER.md`, `TOOLS.md`) are injected into a cron's run on *every fire* — the same reason they're the expensive default for surface routing. A **mechanical** cron whose prompt already carries everything it needs (run a script → fill fields → format a report) re-pays that cost for context it never reads, so it routes with `--light-context` to skip bootstrap injection. A cron whose quality depends on persona/tone or an `AGENTS.md` rule it doesn't restate keeps the **full** context. This is decided independently of which model runs the job (a job can be cheap-model *and* full-context, or default-model *and* light) — the default is light for a purely mechanical `agentTurn`, full when persona/root-rule dependence is real, and *when unsure, full wins* (correctness over a few tokens). The flag is a no-op for `command`/`system-event` jobs, which inject no root context to begin with.

### Automation (hook routing)

> "Whenever a message comes in, log the sender"
> "Snapshot memory every time I run /new"
> "Inject a reminder at every session start"

dinomem classifies the request in two stages — first the **surface** (a react-only side effect → an internal hook; something that must *block/cancel/rewrite* → it tells you that needs a typed plugin hook instead), then the **event** from OpenClaw's closed set of lifecycle events. It scaffolds a vetted `handler.ts` + `HOOK.md` (you fill in only the gate/action logic — never hand-write the boilerplate), keeps a cheap deterministic check first so hooks stay near-zero-cost, and enables it through `openclaw hooks enable`. Every hook is confirm-before-write since all of them change runtime behavior.

### Teaching procedures (skill routing)

> "Here's how I want you to review a PR — remember this method"
> "When someone asks for a market recap, follow these steps"
> "Learn this deployment checklist and use it whenever I say deploy"

Procedural knowledge that's only needed *sometimes* doesn't belong in a root file (which loads every turn) — it belongs in a **skill**, read on-demand when its task appears. dinomem scaffolds a `SKILL.md` (name + a `description` that acts as the trigger + a machine-readable body) and, only if the description alone is too weak to fire reliably, adds **one line** to `AGENTS.md` as a hard pointer. The body stays in the skill, loaded on-demand — never inlined into a root file. Installed through `openclaw skills install`, pinned to the resolved agent so it never leaks into the wrong workspace. Confirm-before-write, since a skill changes what the agent can do.

### Surface arbiter (which mechanism, not just which file)

The four routers above overlap at the edges — "always log X" could be a hook *or* an AGENTS.md rule; "do Y every morning" is a cron, not a rule. So before any write, `tools/route.py` decides **which surface** the request belongs to. The rule is simple: **put behavior where its trigger lives, and only fall back to a root file when the behavior has no trigger** — because root files are injected into context on *every single turn*, so they're the most expensive home. The arbiter doesn't rank surfaces by importance; it **matches the request to the surface that fits it**, and root files are the fallback of last resort:

| If the request… | …it belongs to | Always-on cost |
| --------------- | -------------- | -------------- |
| runs on a schedule/interval/date | **cron** | none (schedule-gated) |
| reacts to a gateway event | **hook** | none (event-gated) |
| is on-demand procedure needed *sometimes* | **skill** | ~1-line trigger + on-demand body |
| is always-on with **no trigger** — identity, style, a tool spec, or an unconditional rule/behavioral preference (a biographical user fact routes to a memory source, not a root file) | **root file** *(fallback)* | full — injected every turn |

The first three are trigger-gated, so they cost nothing until their trigger actually fires — that's *why* they're preferred whenever a request has a trigger, not because they outrank root files in importance. Only when a request has no schedule, no event, and isn't on-demand does it belong in a root file at all.

**Inside the root-file fallback**, the hand-maintained files carry **equal weight** — there's no ranking among them, just the right home for each content type: name/role → `IDENTITY.md`, tone/style → `SOUL.md`, tool specs → `TOOLS.md`, and **SOPs / rules / behavioral preferences / `when_to_use` → `AGENTS.md`** (its correct, first-choice home, not a last resort). Biographical user facts do **not** go to a root file — they route to a memory source (`memory/ _pin_` or `memory/peers/` rep), which `extract_user.py` distills into a peer rep and `compile_user.py` then assembles into `USER.md`'s managed router block (owner block + user map). `USER.md` and `MEMORY.md` are never write targets — the router hard-excludes both. "What's your name" is `IDENTITY.md`; "log every inbound message" is a **hook**, not a rule; "never reveal secrets" — no trigger, an unconditional rule — belongs in `AGENTS.md`, exactly where it should.

The arbiter reasons through ordered discriminators (time trigger → event trigger → on-demand body → identity/style → user fact → tool spec → unconditional rule) and routes to exactly one leaf tool — where a *biographical user fact* resolves to a memory source (`_pin_`/peer rep), never to `USER.md` itself. It's write-free — it only emits the machine-readable decision schema (`route.py classify`) for the agent to reason over.

---

## Prerequisites

- [ ] [OpenClaw](https://github.com/openclaw/openclaw) **>= 2026.1.0** installed and running (`openclaw status` / `openclaw --version`). The `memorySearch`, `compaction`, and `contextInjection` config keys dinomem patches require 2026.1.0 or newer.
- [ ] Python 3.8+
- [ ] Linux or macOS (Windows: use WSL2)
- [ ] [Docker](https://docs.docker.com/get-docker/) — **auto-installed for you (recommended for full capacity).** Powers the local embedding server (TEI), which drives semantic recall/dedup. The installer **auto-installs Docker on Linux** by default, so you normally do nothing. It's not a hard blocker: if Docker genuinely can't run on your box, dinomem **degrades gracefully** — core memory (auto-save + `memory_search`) keeps working, but semantic recall runs slower/weaker until TEI is available. Recommended: let it install. (macOS only: install Docker Desktop yourself — it can't be scripted headlessly.)

### Minimum spec

The local TEI embedding server (CPU image, `intfloat/multilingual-e5-small`, multilingual EN/ID + majors, 384-dim) is the sizing driver.

| Resource | Minimum | Comfortable |
| -------- | ------------ | ----------- |
| CPU      | 2 vCPU       | 2–4 vCPU    |
| RAM      | 2 GB         | 4 GB        |
| Disk     | 5 GB free    | 10 GB       |

The installer preflight checks these automatically: **RAM/CPU below minimum → warning + continue** (TEI may OOM under batch load); **free disk below a 2 GB hard floor → install blocks** unless you pass `--force` (the TEI image pull will otherwise fail mid-install).

**Agent-driven installs.** A warning printed to stdout is easy for an automated caller to skim past, so the preflight also emits machine-readable signals: `DINOMEM_PREFLIGHT_WARN=...` (below-recommended RAM/CPU/disk, continues) and `DINOMEM_PREFLIGHT_BLOCK=...` alongside `exit 1` (disk hard floor). An agent running the installer should **surface any `[AGENT-NOTICE]` / `PREFLIGHT_WARN` to the user before proceeding**, and treat the `exit 1` disk block as a hard stop — **do not auto-retry with `--force`** (that flag means "I accept the risk," not "retry harder").

### Token usage

dinomem's only recurring cost is LLM tokens. Estimated tokens/month (input+output), grounded in the actual batch caps:

| Usage | Tokens/mo | Non-reasoning? |
| ----------------------- | --------- | -------------- |
| Low — ~2–3 sessions/day | ~1–2M     | ✅ all          |
| Moderate — ~8–12/day    | ~5–9M     | ✅ all          |
| High — ~30+/day         | ~18–30M   | ✅ all          |

**Every dinomem base LLM call is non-reasoning bulk** (extraction, review) — so **100% of these tokens are non-reasoning.** Set [`DINOMEM_CHEAP_MODEL`](#tuning-guide-manual-strongly-recommended) and all of it routes to your cheap high-context model.

_Grounding: extract ~26k tok / 3 sessions · review one ~4k-token call/day · order-of-magnitude, scales with sessions/day + corpus size._

---

## Quick Start

```bash
git clone https://github.com/02-dino/dinomem
bash dinomem/scripts/install.sh \
  --workspace ~/.openclaw/workspace-myagent \
  --agent-id myagent
openclaw gateway restart
```

That's it. The installer handles Docker, cron, config patches, and AGENTS.md wiring.

> **Don't know your agent ID or workspace path?**
> Run `openclaw agents list` — it shows all agents and their workspace paths.

---

## How do I know it's working?

```bash
# 1. TEI embedding server is running
curl http://localhost:8080/health
# → {"status":"ok"}

# 2. Cron is registered
crontab -l | grep auto_session_reset

# 3. Run first extraction manually
python3 ~/.openclaw/workspace-myagent/procedures/auto_session_reset.py
```

After a session is archived and extracted, you'll see new files in `memory/` and entries in `MEMORY.md`.

### Verify it actually works — on demand

Curious whether the memory is real, correct, and reversible? These are the tools to reach for. Nothing here runs in the background you have to watch — you check when *you* want to:

```bash
# WHAT CHANGED + UNDO IT — every add/update/delete is stamped with a restore_ref
# (a git HEAD sha), so any memory change is byte-exact reversible.
python3 procedures/_memory_diff.py             # audit log of recent memory changes
# each entry carries a restore_ref you can git-restore to undo that exact change

# MEMORY AT A GLANCE — zero-LLM counts/health card (also the Sunday cron)
python3 scripts/weekly_stats.py --workspace .  # how many facts/insights, recent activity
```

- **Transparent + reversible.** `_memory_diff.py` is the audit trail: see *what* the memory did, and *undo any change byte-exact* via its `restore_ref`. Nothing is a black box.
- **Prove recall quality (citable).** Want a hard number on how well the memory answers — on the same benchmarks the papers use? The harness in [`benchmark/longmemeval/`](benchmark/longmemeval/) measures dinomem with the official scorer, in an isolated throwaway workspace (your real memory is never touched). It runs **two public datasets** via `--dataset`: **LongMemEval-S** (`--dataset longmemeval`, the default — ~500 long-haystack questions) and **LoCoMo** (`--dataset locomo` — ~1,986 questions over long multi-session conversations). Same protocol, same scorer, either dataset. That's the on-demand "does it actually work" proof.
- **Run the WHOLE evaluation with one command.** Beyond that single benchmark, dinomem ships a full multi-phase evaluation program — standard benchmarks *plus* the harder questions a memory system should answer (does it stay correct as facts change? does the corpus stay compact? does it resist poisoning?). You don't need to know the phase order or per-runner flags; there's one front door:

```bash
# See the whole plan first — free, no spend, no lab touched:
python3 benchmark/run_all.py --source . --dry-run

# Free floor only (RAG arm needs no LLM):
python3 benchmark/run_all.py --source . --arms rag

# Full comparison (needs a real model to build/answer memory):
python3 benchmark/run_all.py --source . \
    --answer-model gpt-4o-mini --judge-model gpt-4o
# → runs every phase in order, then writes benchmark/scorecard/results/scorecard.md
```

  It runs each phase in dependency order (standard → longitudinal → supersession/dedup → pattern/promotion/behavior → poisoning → ablation) and renders one unified scorecard that puts **quality next to its cost/latency/storage** — no advantage claim without its price. `--dry-run` and `--estimate-only` never spend; a partial run still produces a valid report (anything unrun is listed honestly, not hidden).

  **Base-only installs degrade gracefully.** The `neuron` arm and the two neuron-only phases (4b promotion, 5b ablation) require the neuron overlay — a different pipeline (L2 graph / L3 synthesis+contradiction / L4 promotion) with its own retrieval tool (`hybrid_recall`). If you don't pass `--overlay-cmd`, `run_all.py` **auto-drops the neuron arm and skips 4b/5b with a printed reason** rather than failing; you still get the full rag-vs-base comparison. Add `--overlay-cmd` later to light up the neuron arm and those phases.

> Deeper introspection — *why* a specific memory is here and what lifecycle state it's in — lives in **dinomem-neuron** (`explain_memory.py`, `lifecycle_state.py`).

---


## Installing dinomem

| Flag | Default | Description |
|------|---------|-------------|
| `--workspace DIR` | `$OPENCLAW_WORKSPACE` or `~/.openclaw/workspace` | Path to agent workspace |
| `--agent-id ID` | Detected from workspace name | OpenClaw agent ID |
| `--no-docker` | — | Skip TEI Docker setup. **Not recommended** — memory still auto-saves and `memory_search` works, but you lose fast local semantic recall/dedup (runs slower/weaker until TEI is available). Only use on a box that genuinely can't run Docker. |
| `--no-cron` | — | Skip crontab registration |
| `--repair-cron` | — | **Idempotent "just fix the crons" mode.** Skips every heavy/one-time phase (Docker, file copy, config wiring) and jumps straight to cron registration + self-check. Safe to re-run any time a prior install left the note-lifecycle crons unregistered. |
| `--no-backup-cron` | — | Skip weekly backup cron (if you have your own backup system) |
| `--force` | — | Overwrite existing scripts |
| `--dry-run` | — | Preview every change without writing anything (no files, crons, Docker, or config patch). Idempotency-aware: reports `[plan]` for new actions, `[skip]` for what already exists. Re-run without the flag to apply. |

---

## What gets installed

```
<workspace>/
├── procedures/
│   ├── auto_session_reset.py   # Cron entry point — runs every 15 min
│   ├── session_reset.py        # Archives old/compacted sessions
│   ├── extract_memory.py       # Extracts memories from archives via LLM
│   ├── memory_cleanup.py       # Daily dedup of memory files
│   ├── memory_review.py        # Daily batched LLM review (valid/invalidated/noise)
│   ├── workspace_backup.py     # Weekly snapshot backup (keep 3, auto-rotate)
│   ├── git_history.py          # git-backed state API (last-touched, commit-count, content_at, restore) — read-only, fail-open
│   ├── memory_retention.py     # git-aware age/reinforcement check — spares recently-touched files from pruning
│   └── _memory_diff.py         # audit log + git restore_ref (byte-exact undo of any add/update/delete)
├── tools/
│   ├── route.py               # Surface arbiter — cost-ordered decision schema (cron>hook>skill>root); write-free
│   ├── config_tool.py          # Safe writer for root config files (agent self-config)
│   ├── cron_tool.py            # Intent router + safe writer for cron jobs (via `openclaw cron`)
│   ├── hook_tool.py            # Intent router + scaffolder for event hooks (via `openclaw hooks`)
│   ├── skill_tool.py          # Intent router + scaffolder for skills (SKILL.md + thin trigger, via `openclaw skills`)
│   └── gate/                   # Portable pure-shell T1 gates (file-changed, threshold, diff-since-last)
├── templates/
│   └── hook.handler.ts.tmpl    # Vetted hook scaffold (fill gate/action blanks only)
├── logs/
└── memory/
    ├── _pin_*.md               # Permanent user-pinned memories (never deleted)
    ├── _note_*.md              # Transient todos/reminders (resolved via done_when, GC'd via stale_after)
    └── YYYY-MM-DD_<type>_<slug>.md  # Per-item memory files (auto-generated, one file per extracted item)
MEMORY.md                       # Searchable index (auto-generated, do not edit)
```

---

## Cron schedule

| Time | Script | What runs |
|------|--------|-----------|
| Every 15 min | `auto_session_reset.py` | Session archive + memory extraction |
| Every 15 min *(default-on, isolated)* | `git-autosnapshot/auto-commit.sh` | Git-snapshot memory + config into `.dinomem-snap.git`; disk-aware self-cleanup |
| Daily 5:00 UTC | `memory_cleanup.py` | Dedup memory files |
| Weekly Sun 2:00 UTC | `workspace_backup.py` | Snapshot backup (keep 3) |
| Daily 5:30 UTC | `memory_review.py` | LLM review — batched, full cycle ~7 days |

---

## Git-versioned memory (git-autosnapshot)

**On by default** *(opt out with `--no-git-snapshot`)*. Two things, one substrate: a git snapshot layer, and memory logic that **reads git as live truth**.

> **Isolation — why it's safe to default on.** The snapshot object DB lives in a **separate git-dir**, `.dinomem-snap.git`, addressed via `--git-dir`/`--work-tree`. It **never touches your own repo**: it does *not* run `git init` in-place, does *not* drop a `.gitignore`/`.gitattributes` into your working tree (ignore rules go in the snapshot store's private `info/exclude`), and never reads or writes your `~/.openclaw/.git` if you keep one. You can `git init` and commit `~/.openclaw` yourself and never know ours is there. Because collision is impossible by construction, it earns default-on.

### 1. The snapshot layer

A lightweight timer runs `features/git-autosnapshot/auto-commit.sh` (every 15 min) and commits every non-ignored change — your `MEMORY.md`, `memory/*.md`, notes, pins, skills, and configs — **into the isolated store**. Runtime churn (`*.sqlite*`, `kb/vector_db/`, `memory/cache/`, `logs/`, `models/`) is ignored, so the store stays small.

- **Byte-exact undo.** A bad edit, dedup, or semantic merge is reversible with one command: `git --git-dir=~/.openclaw/.dinomem-snap.git --work-tree=~/.openclaw checkout <ref> -- memory/`.
- **Size guard (LFS-aware).** New **non-LFS** files over `AUTOSNAP_MAX_MB` (default 10) are refused from staging (they stay on disk) — a stray model/`.jsonl`/`.sqlite` dump can never bloat the store. **LFS-tracked** files (media/archives/pdf) are exempt: their bytes live outside history, so a 40 MB `.mp4` is added *via* LFS instead of being dropped. To version a specific oversized **non-LFS** blob anyway, list its glob in a `.dinomem-keep-large` file at the repo root (see [git-autosnapshot README](features/git-autosnapshot/README.md#keeping-an-oversized-non-lfs-file-on-purpose)).
- **Disk-aware self-cleanup.** Housekeeping escalates as the disk fills:

  | Disk | Action |
  |------|--------|
  | `<80%` HEALTHY | light `gc --auto` + `lfs prune`, ~hourly |
  | `80–89%` WARN | `gc --prune=now` + `lfs prune` + collapse snapshots older than `RETAIN_DAYS`, every tick |
  | `≥90%` EMERGENCY | reflog expire + aggressive `gc` + force `lfs prune` + 7-day retention |

- **Hand-written commits are permanent** — only `auto-snapshot` commits ever collapse into a baseline under retention.
- **Local-only, fail-open.** No remote by default (add one for durability); every git call degrades to a benign no-op if git is missing.

### 2. Git as a live signal (`git_history.py`)

The payoff isn't just rollback. Once history exists, `procedures/git_history.py` exposes it through a tiny stdlib API (`file_first_seen`, `file_last_touched`, `commit_count`, `content_at`, `diff_since`, `restore`) — **read-only, fail-open, git-optional**. Memory components read git instead of remembering (or guessing) state:

| Consumer | Uses git for |
|----------|-------------|
| `memory_retention.py` | Before pruning a file, checks `file_last_touched` — a **recently-reinforced** memory is spared. `commit_count` = a recurrence/importance signal. Replaces unreliable mtime. |
| `_memory_diff.py` | Stamps each add/update/delete with a `restore_ref` (HEAD sha) → byte-exact reversible. |
| `memory_cleanup.py` | After a dedup/merge pass, prints `undo this run: git checkout <sha> -- memory/`. |
| `resolve_done_notes.py` | Snapshot-before-delete; a resolved note is recoverable via `git checkout <head>:memory/<note>`. |

> **Design rule:** don't *remember* STATE — compute it from git live. "Did this ship? are two copies in sync? last-touched?" → `git log`/`diff`/`git_history.py`, never a stored note. Git can't go stale, because it's recomputed from reality on every read.

So the memory-quality win is **retention**: hot notes stay, cold notes age out, and every destructive pass is reversible.

### Install / uninstall

The main installer sets it up **automatically** (default-on). To opt out:

```bash
bash scripts/install.sh --workspace ~/.openclaw/workspace-myagent --no-git-snapshot
```

Or manage the feature directly:

```bash
bash features/git-autosnapshot/install.sh --repo ~/.openclaw               # isolated store + timer
bash features/git-autosnapshot/install.sh --repo ~/.openclaw --uninstall   # remove timer (store + your files kept)
```

`git_history.py` itself needs nothing enabled — it auto-detects the `.dinomem-snap.git` store and degrades cleanly when it's absent, so the retention/undo consumers work with or without the snapshot timer running.

---

## Compatibility

dinomem is designed for a default OpenClaw setup. If your agent is already customized, read this before installing.

| Potential clash | What happens | How to avoid |
|----------------|-------------|-------------|
| Custom `session.reset` config | install.sh warns and keeps your existing value | Nothing — your config is preserved |
| Custom `memorySearch.provider` | install.sh warns and skips TEI wiring | Wire TEI manually after install |
| Port 8080 in use | install.sh warns, copies docker-compose but does not start TEI | Change port in `docker-compose.tei.yml` or use `--no-docker` |
| Existing `kb/vector_db/` | install.sh warns — dinomem will write to this path | Back up first, or use a separate workspace |
| Existing `memory_recall` in AGENTS.md | install.sh warns — block will be appended | Remove duplicate manually after install |
| Existing backup system | Weekly backup cron may be redundant | Use `--no-backup-cron` to skip |
| Native Codex plugin active | OpenClaw skips raw `MEMORY.md` injection and uses a memory pointer instead — breaks dinomem's always-injected guarantee | Do not activate `plugins.entries.codex` when using dinomem. No config override exists — this is hardcoded in OpenClaw internals. |
| OpenClaw Dreaming enabled | Dreaming writes its own memory extractions to `memory/` — conflicts with dinomem's `extract_memory.py` which writes to the same folder. Both may overwrite each other. | Disable Dreaming manually before installing dinomem. install.sh cannot force this off — Dreaming is a separate feature and must be disabled independently in your OpenClaw config. (Note: dinomem's `memoryFlush` is the guarded bare-daily writer for startupContext and is unrelated to Dreaming.) |

> If your agent has heavy customization, run `bash scripts/install.sh --dry-run` first to preview every change (files, crons, Docker, config patch) without writing anything, then re-run without `--dry-run` to apply.

---

## OpenClaw config patches

The installer automatically patches `~/.openclaw/openclaw.json`:

| Config | Value | Reason |
|--------|-------|--------|
| `session.reset.mode` | `idle` | Prevent premature daily resets |
| `session.reset.idleMinutes` | `10080` | Reset only after 7 days of inactivity |
| `contextPruning.mode` | `off` | Compaction summarizes — TTL pruning just drops |
| `compaction.mode` | `safeguard` | Summarizes before dropping context |
| `compaction.truncateAfterCompaction` | `true` | Enabled — successor transcript prevents unbounded JSONL growth. `session_reset.py` now tracks compaction depth via `parentSession` chain traversal instead of `compactionCount`, so this is safe. Predecessor JSONLs are archived immediately on reset (no 48h orphan delay). |
| `compaction.memoryFlush.enabled` | `true` | Enabled as a guarded writer of the bare daily file `memory/YYYY-MM-DD.md` that feeds `startupContext`. A prompt override confines it to that file and forbids touching `MEMORY.md`. |
| `memorySearch.provider` | `openai-compatible` | Use local TEI server |
| `memorySearch.remote.baseUrl` | `http://localhost:8080/v1` | TEI Docker endpoint |
| `agents.defaults.contextInjection` | `always` | Root files (AGENTS.md, SOUL.md, etc) injected every turn — not skipped on continuation turns. (This is already the OpenClaw default; set explicitly to document intent. The valid key is `contextInjection` — earlier dinomem versions wrote an invalid `workspaceBootstrap` key that crashed the gateway; install/uninstall now strip that legacy key automatically.) |
| `startupContext.enabled` | `true` (`dailyMemoryDays: 2`) | Injects the last 2 days of bare daily memory on `/new` and `/reset`. `memoryFlush` writes those bare `YYYY-MM-DD.md` files (separate namespace from dinomem's `_`-suffixed extraction files, so no clash); `cleanup_startup_daily.py` prunes them past the window. `memory_search` pull still handles deep recall. |
| `agents.defaults.thinkingDefault` | `medium` floor (explicit low values only) | Ensures the agent genuinely internalizes and acts on instructions in root files (AGENTS.md, SOUL.md, MEMORY.md, etc.) rather than skimming past them. Without a minimum thinking floor, injected behavior rules and memory context may be acknowledged but not reliably followed. **True floor, raise-only, and only touches *explicit* below-floor values:** lifts to medium only if you have explicitly set `off`/`minimal`/`low`. `medium`/`high`/`xhigh`/`adaptive`/`max` are left untouched (never clobbered down). **Unset is deliberately left alone** — an unset `thinkingDefault` resolves to your *model's* default (per OpenClaw's thinking resolution: Claude 4.6 → `adaptive`, Opus 4.8/4.7 → `off`), and install.sh can't know your model, so forcing `medium` on unset would risk lowering a 4.6 user's `adaptive` default. We respect the model default instead. If you want the floor guaranteed, set `thinkingDefault` explicitly. |
| `agents.defaults.bootstrapMaxChars` | raised to fit (default `20000`) | Per-file injection cap. install.sh measures the largest root file *after* injecting dinomem's blocks and raises this to `max(existing, 20000, largest_file + 10000)` so the always-injected files are never silently truncated. Raise-only: never lowers your value, never shrinks below the default. Idempotent + order-independent (measured, not `current + delta`), so reinstalling or stacking dinotrust converges to one buffer, not two. |
| `agents.defaults.bootstrapTotalMaxChars` | raised to fit (default `60000`) | Total cap across all root files. Same raise-only logic: `max(existing, 60000, total_root_files + 10000)`. The cap is a ceiling, not injected size — headroom costs nothing until used. Single files over `100000` trigger a sanity warning (advising a trim) but do not block. |
| `tools.sessions.visibility` | `all` | Allows cross-agent `sessions_send` and `sessions_history`. Default `tree` only covers the current session + its spawned subagents — blocks sending to other agents. Set to `all` so dinomem's memory pipeline can reach across agent boundaries. Requires `tools.agentToAgent` to be enabled for cross-agent calls. |
| `tools.deny` / `tools.allow` | remove `sessions_spawn` from deny; add to allow if explicit allowlist exists | dinomem-neuron's Project Advancer relies on `sessions_spawn` to delegate bounded sub-tasks. If denied or missing from an explicit allowlist, project execution silently falls back to single-turn inline work and overflows context. install.sh removes it from deny and adds it to allow when an explicit allowlist is present (empty allow = no restriction, no patch needed). |
| `agents.defaults.timeoutSeconds` / `…subagents.runTimeoutSeconds` | floor of `300`s (5 min) | Heavy multi-step turns and research-then-build steps (especially dinomem-neuron's Project Advancer, which runs long inline steps and spawns sub-agents) can otherwise trip an `LLM request timed out` mid-turn on slower providers. 300s is a deliberate middle ground: enough headroom for a heavy step, short enough that a genuinely hung request still surfaces without an endless wait. **Raise-only** — never lowers a higher value you set. On very slow/self-hosted models (local Ollama, llama.cpp) you may want to raise it further; on fast hosted APIs the floor rarely engages. Provider-level `models.providers.<id>.timeoutSeconds` is left untouched (provider-specific — your call). |

See [`references/openclaw-config-snippet.json5`](references/openclaw-config-snippet.json5) for the full annotated config.

## Tuning guide (manual, strongly recommended)

Not patched automatically — skipping these hurts cost, performance, response speed, and memory quality. Set based on your model.

See [docs/TUNING.md](docs/TUNING.md)

---

## Troubleshooting

**Quick health check**
```bash
bash scripts/doctor.sh
# Checks TEI /health + /info, reports model_id + max_input_length.
# Exit 0 = healthy, exit 1 = unreachable/unhealthy (prints likely cause).
# Use --url to point at a non-default endpoint, --quiet for scripting.
```

**TEI server won't start**
```bash
docker logs tei-embed
# Common: port 8080 already in use
lsof -i :8080
docker start tei-embed
```

**Memory not being extracted**
```bash
tail -50 ~/.openclaw/workspace-myagent/logs/extract_memory.log
python3 ~/.openclaw/workspace-myagent/procedures/extract_memory.py
```

**Cron not running / note-lifecycle crons missing**
```bash
crontab -l | grep auto_session_reset
openclaw cron list --json | grep -i "Note Cron Gate"   # the note-janitor gate
# If any lane is missing, run the fast idempotent cron-only repair (no re-copy, no Docker):
bash dinomem/scripts/install.sh --workspace ~/.openclaw/workspace-myagent --agent-id myagent --repair-cron
systemctl status cron      # Ubuntu/Debian
systemctl status crond     # CentOS/RHEL
```
> The installer now **auto-verifies its required crons and self-repairs** on a normal run; `--repair-cron` is the manual escape hatch if a lane is still missing (e.g. the gateway rejected command-kind crons — grant `operator.admin` and re-run).

**`memory_search` not finding anything**
```bash
curl http://localhost:8080/health
wc -l ~/.openclaw/workspace-myagent/MEMORY.md
python3 ~/.openclaw/workspace-myagent/procedures/extract_memory.py
```

**`openclaw` command not found**
```bash
export PATH="/home/linuxbrew/.linuxbrew/bin:$PATH"
which openclaw || find /usr /home -name openclaw 2>/dev/null
```

---


## FAQ

See [docs/FAQ.md](docs/FAQ.md)

---

## Update

```bash
bash dinomem/scripts/update.sh --workspace ~/.openclaw/workspace-myagent
```

## Uninstall

```bash
bash dinomem/scripts/uninstall.sh --workspace ~/.openclaw/workspace-myagent --agent-id myagent
```

This removes: cron jobs, AGENTS.md block, openclaw.json patches, TEI Docker container.

Optional flags:
- `--purge` — also remove installed scripts
- `--purge-data` — remove `logs/` and snapshots (memory is preserved)
- `--purge-memory` — ⚠️ permanently delete `memory/` and `MEMORY.md` (requires typing `wipe memory` to confirm)

Run `openclaw gateway restart` after uninstall to apply config changes.

---

---

## Want more? → dinomem-neuron (private repo)

dinomem remembers.
**dinomem-neuron learns.**

dinomem gives your agent memory. Neuron turns those memories into behavioral knowledge that persists across every future conversation — without you writing a single config line.

> 📖 New to the difference? Read the plain-language [**Before / After**](BEFORE_AFTER.md) — it ends with a base-vs-neuron one-liner that makes the split click.

---

### What that actually means

With dinomem alone:

```
Session → Archive → Extract → memory/*.md
                                    ↓
                              MEMORY.md index
                              (injected every turn — navigation map)
                                    ↓
                         Agent searches memory on demand
```

With neuron:

```
Session → Archive → Extract → memory/*.md
                                    ↓
                              MEMORY.md index
                              (injected every turn — navigation map)
                                    ↓
                         Agent searches memory on demand
                                    +
                            Relationship graph
                                    ↓
                            Pattern synthesis
                                    ↓
                            Contradiction check
                                    ↓
                        Promoted to permanent knowledge
                                    ↓
                        Injected every turn → Behavior change
```

dinomem's `MEMORY.md` tells the agent **what exists** in memory — a navigation map, injected every turn. Neuron extends it: promoted insights are also injected every turn, but as **behavioral knowledge**, not just a map. Same file, different content.





---

### Before / After

These are real memory entries extracted from separate sessions over several weeks:

```
2026-05-26: "The 'full_analysis_workflow' for analytical queries mandates
             calling ALL workspace tools, regardless of perceived necessity."

2026-05-27: "Framework validation: The rule for mandatory tool usage in
             analytical queries was confirmed and explicitly stated as
             'Always use all available tools, regardless of necessity.'"

2026-05-28: "Informational queries use only relevant tools. Analytical
             queries trigger full_analysis_workflow that mandates ALL
             available workspace tools."

2026-05-31: "Analytical path (full_analysis_workflow) mandates the use
             of all available tools."
```

**Neuron's L3 synthesis output:**

```
insight:          "Agent consistently enforces a strict tool-usage rule:
                   analytical queries must call ALL workspace tools without
                   exception. This rule has been validated across 4+
                   independent sessions."
confidence:       0.94
convergence:      4 clusters
first_seen:       2026-05-26
reinforcement:    4 independent runs
contradictions:   none
lifecycle:        stable
status:           provisional → trusted
```

**After L4 promotion** — this insight is written into `MEMORY.md` and injected every turn. The agent no longer needs to be reminded of the rule. It's baseline behavior. No prompting. No configuration. No manually written rule. The agent learned it.

> 📖 Want the plain-language version of this leap — what it *feels* like to go from base to neuron? Read the full [**Before / After**](BEFORE_AFTER.md#-and-if-you-want-it-to-learn-dinomem-neuron).





---

### What neuron adds

| Layer | What it does |
|-------|--------------|
| **Relationship Discovery** | Identifies relationships between memories across conversations — explicit relation extraction, entity nodes, forward reference detection, and graph traversal for multi-hop queries |
| **Pattern Synthesis** | Analyzes groups of related memories and generates candidate insights. Skeptical by design — a pattern must emerge independently more than once. |
| **Contradiction Resolution** | Prevents conflicting beliefs from becoming permanent knowledge. Conflicts are held back until resolved. |
| **Knowledge Promotion** | Insights that demonstrate stability over time become persistent knowledge. A single observation is never enough. |
| **Enriched navigation index** | `MEMORY.md` serves dual roles: search navigation aid (like dinomem base) + always-present behavior layer. Promoted insights live here — active every turn without recall. |
| **Long-document RAG** | Contracts, books, legal text — plus images, scanned pages, audio, video, ZIPs, and video links all ingest automatically. Documents and media are transcribed or OCR'd by the agent's own vision model (no GPU, no setup). Stored separately, never pollutes memory, searchable via `docs_search` |
| **Structured data** | Spreadsheets, exports, CSV/JSON/Parquet — exact answers to *how many, which ones, under $X, grouped by* that embeddings can't give, plus forgiving value lookup and "find rows about X" over free-text columns. Stored separately, never pollutes memory, searchable via `data_query` |
| **Automatic notes** | The agent writes `_note_` files from its own commitments and task follow-ups — not only when you ask |
| **Project execution** | Large builds become step-by-step plans the agent works through one step at a time across sessions, advancing on its own and pausing for approval on anything risky |
| **Self-improving closer** | When a project finishes, the agent reviews its own work, makes a safe, behavior-preserving improvement if one helps (or confirms none is needed), then retires the completed note automatically — the workspace cleans up after itself |
| **Daily Note Review** | A daily janitor cron retires *any* resolved note — `task_bound` notes whose `done_when` is met, projects finished out-of-band, and abandoned notes past `stale_after` — so completed work doesn't pile up. Claim-aware: never touches a note another job or a live session is actively editing. |
| **Behavior Promotion** | Reusable patterns distilled from completed projects, memory patterns, and best practices — then routed to the right surface: an on-demand procedure becomes a **skill** (auto-written), an event-driven pattern becomes a **hook**, a scheduled one becomes a **cron** (both staged for confirmation, safety-validated so a promoted hook can't loop or silently break the agent, and deduped against what's already installed so nothing double-fires) |
| **Bi-temporal memory** | Facts carry a validity window — when they started being true and when they stopped. Change your mind and the old belief is *retired to history* (never deleted), not left to fight the new one; ask "what did I think back in June?" and it can answer *then* separately from *now*. Same-subject updates are recognized as supersession, not flagged as contradictions. Memory becomes a timeline, not a flat pile. |
| **Explainable + calibrated** | Ask *why* a memory is there and get a straight answer — its lifecycle state, when it was recalled, and the evidence behind any promoted insight. Promotion confidence is calibrated against a track record, so "trusted" means measured-reliable, not just asserted. |
| **Session deep recall** | When memory summary is thin, searches raw archived sessions (21-day window) for the exact exchange — sharper, more detailed recall for recent context |
| **Git-anchored provenance** | Builds on base's git-versioned memory (an isolated `.dinomem-snap.git` store, on by default, that never touches your own repo): when neuron promotes an insight to `MEMORY.md`, distills a skill, or stages a runtime-mutating hook/cron proposal, it stamps the record with the current `HEAD` sha — so every promoted insight, skill, and staged proposal carries *which commit it came from*. Fail-open: degrades cleanly if the store is absent. The Project Advancer also verifies a project's `done_when` against real git state instead of trusting a stale `resume_state`. |

---

Access granted after onboarding — DM [**@dinotlgrm on Telegram**](https://t.me/dinotlgrm)

> dinomem-neuron install instructions are in the private repo after access is granted.


---

## License

MIT

---

Made with 🦖 by [@02-dino](https://github.com/02-dino)
