# 🦕 dinomem — The Memory Layer That Gets Sharper Over Time

> Self-curating long-term memory for AI agents. Most memory systems bloat with noise — dinomem distills each session, dedupes daily, and recalls before it acts.

An LLM reads each archived session and distills what matters into structured memory files — automatically reviewed daily in batches, deduplicated daily, and updated when things change. The agent is behaviorally wired to search memory before acting, so recall actually happens. Memory quality improves over time.

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

---

> Want your agent to not just remember, but learn?
> dinomem-neuron is a separate private repo — not included here. Nightly pattern synthesis, contradiction detection, and behavioral promotion.
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
- **Weekly snapshot backup** — memory, config, and root files backed up automatically. Keep-3 rotation, never clutters disk. Restore anytime via `workspace_backup.py`.
- **Zero-config install** — one script handles Docker, cron, and OpenClaw config patches



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
  LLM reads archived sessions → extracts facts, decisions, preferences, patterns, lessons
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

dinomem includes a routing system that detects your intent and writes to the correct file automatically — `SOUL.md` for tone, `IDENTITY.md` for persona, `AGENTS.md` for rules and workflows, `TOOLS.md` for tools, `USER.md` for your preferences. Backs up before every write — auto-rotated, keeps last 3 per file, never clutters disk.

---

## Prerequisites

- [ ] [OpenClaw](https://github.com/openclaw/openclaw) **>= 2026.1.0** installed and running (`openclaw status` / `openclaw --version`). The `memorySearch`, `compaction`, and `contextInjection` config keys dinomem patches require 2026.1.0 or newer.
- [ ] [Docker](https://docs.docker.com/get-docker/) — for the local embedding server
- [ ] Python 3.8+
- [ ] Linux or macOS (Windows: use WSL2)

> **Don't know your agent ID or workspace path?**
> Run `openclaw agents list` — it shows all agents and their workspace paths.

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

---


## Installing dinomem

| Flag | Default | Description |
|------|---------|-------------|
| `--workspace DIR` | `$OPENCLAW_WORKSPACE` or `~/.openclaw/workspace` | Path to agent workspace |
| `--agent-id ID` | Detected from workspace name | OpenClaw agent ID |
| `--no-docker` | — | Skip TEI Docker setup |
| `--no-cron` | — | Skip crontab registration |
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
│   └── workspace_backup.py     # Weekly snapshot backup (keep 3, auto-rotate)
├── tools/
│   └── config_tool.py          # Safe writer for root config files (agent self-config)
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
| Daily 5:00 UTC | `memory_cleanup.py` | Dedup memory files |
| Weekly Sun 2:00 UTC | `workspace_backup.py` | Snapshot backup (keep 3) |
| Daily 5:30 UTC | `memory_review.py` | LLM review — batched, full cycle ~7 days |

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

See `references/openclaw-config-snippet.json5` for the full annotated config.

## Tuning guide (manual, strongly recommended)

Not patched automatically — skipping these hurts cost, performance, response speed, and memory quality. Set based on your model.

See [docs/TUNING.md](docs/TUNING.md)

---

## Troubleshooting

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

**Cron not running**
```bash
crontab -l | grep auto_session_reset
# If missing, re-run install:
bash dinomem/scripts/install.sh --workspace ~/.openclaw/workspace-myagent --agent-id myagent
systemctl status cron      # Ubuntu/Debian
systemctl status crond     # CentOS/RHEL
```

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





---

### What neuron adds

| Layer | What it does |
|-------|--------------|
| **Relationship Discovery** | Identifies relationships between memories across conversations — explicit relation extraction, entity nodes, forward reference detection, and graph traversal for multi-hop queries |
| **Pattern Synthesis** | Analyzes groups of related memories and generates candidate insights. Skeptical by design — a pattern must emerge independently more than once. |
| **Contradiction Resolution** | Prevents conflicting beliefs from becoming permanent knowledge. Conflicts are held back until resolved. |
| **Knowledge Promotion** | Insights that demonstrate stability over time become persistent knowledge. A single observation is never enough. |
| **Long-document RAG** | Contracts, books, legal text — stored separately, never pollute memory, searchable via `docs_search` |
| **Automatic notes** | The agent writes `_note_` files from its own commitments and task follow-ups — not only when you ask |
| **Project execution** | Large builds become step-by-step plans the agent works through one step at a time across sessions, advancing on its own and pausing for approval on anything risky |
| **Skill Promotion** | Reusable procedures distilled from completed projects, memory patterns, and best practices; promoted automatically |
| **Session deep recall** | When memory summary is thin, searches raw archived sessions (7-day window) for the exact exchange — sharper, more detailed recall for recent context |

---

Access granted after onboarding → [@dinotlgrm](https://t.me/dinotlgrm)

> dinomem-neuron install instructions are in the private repo after access is granted.


---

## License

MIT

---

Made with 🦖 by [@02-dino](https://github.com/02-dino)
