# 🦕 dinomem — Dino Agent Memory

> Most agent memory systems accumulate noise. dinomem maintains quality over time.

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
  Archives sessions idle for 7 days or after 2 compaction generations; deletes archives older than 7 days
        │
        ▼
[extract_memory.py]
  LLM reads archived sessions → extracts facts, decisions, preferences, patterns, lessons
  Writes to memory/YYYY-MM-DD_<type>_<slug>.md (one file per item) + updates MEMORY.md index

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

Saved as `memory/_note_<slug>.md`. Recalled when you ask "what's on my build list?". Auto-deleted by daily cron once the agent detects it's been done.

> **Note:** Memory is recall-based, not always-on. The agent searches for relevant memories when needed — nothing is automatically injected into every turn.



### Agent self-configuration

Not sure where to put something? Just tell your agent:

> "Be more concise"
> "Your name is Aria"
> "Always check X before doing Y"
> "I built a script that does Z, add it as a tool"

dinomem includes a routing system that detects your intent and writes to the correct file automatically — `SOUL.md` for tone, `IDENTITY.md` for persona, `AGENTS.md` for rules and workflows, `TOOLS.md` for tools, `USER.md` for your preferences. Backs up before every write — auto-rotated, keeps last 3 per file, never clutters disk.

---

## Prerequisites

- [ ] [OpenClaw](https://github.com/openclaw/openclaw) installed and running (`openclaw status`)
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
    ├── _note_*.md              # Transient todos/reminders (auto-deleted when resolved)
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

> If your agent has heavy customization, run `bash scripts/install.sh --no-docker --no-cron` first to inspect what would change, then apply cron and Docker manually.

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
| `compaction.memoryFlush.enabled` | `false` | **Must stay disabled** — memoryFlush triggers its own compaction + memory write which clashes with `auto_session_reset.py` |
| `memorySearch.provider` | `openai-compatible` | Use local TEI server |
| `memorySearch.remote.baseUrl` | `http://localhost:8080/v1` | TEI Docker endpoint |
| `agents.defaults.workspaceBootstrap` | `always` | Root files (AGENTS.md, SOUL.md, etc) injected every turn — not skipped on continuation turns |
| `startupContext.enabled` | `false` | Disable startup push-injection of recent memory files — dinomem uses `memory_search` pull instead, which is more precise and scales better |

See `references/openclaw-config-snippet.json5` for the full annotated config.

### Compaction tuning (manual, strongly recommended)

Not patched automatically — skipping these hurts performance, response speed, and memory quality. Set based on your model.

**`reserveTokens`** — set to `contextWindow - 200000` (skip if your model is 200k or under). Keeps active context below 200k, which fixes three things: context bloat, response speed (inference slows non-linearly above 200k), and memory quality (leaner sessions = better compaction summaries).

Examples: 200k model → `50000`, 1M model → `800000`, 128k model → skip.

**`keepRecentTokens`** — set to 25% of `min(contextWindow, 200000)`. Minimum tokens preserved from the most recent window during compaction — protects immediate context continuity.

Examples: 200k model → `50000`, 128k model → `32000`, 1M model → `50000`.

Set both under `agents.defaults.compaction` in `openclaw.json`. See `references/openclaw-config-snippet.json5` for annotated examples.

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
| **Relationship Discovery** | Identifies relationships between memories — even across different conversations and time periods |
| **Pattern Synthesis** | Analyzes groups of related memories and generates candidate insights. Skeptical by design — a pattern must emerge independently more than once. |
| **Contradiction Resolution** | Prevents conflicting beliefs from becoming permanent knowledge. Conflicts are held back until resolved. |
| **Knowledge Promotion** | Insights that demonstrate stability over time become persistent knowledge. A single observation is never enough. |
| **Long-document RAG** | Contracts, books, legal text — stored separately, never pollute memory, searchable via `docs_search` |
| **Calendar integration** | `_note_` reminders linked to Google Calendar, auto-deleted when the event passes |
| **Session deep recall** | When memory summary is thin, searches raw archived sessions (7-day window) for the exact exchange — sharper, more detailed recall for recent context |

---

Access granted after onboarding → [@dinotlgrm](https://t.me/dinotlgrm)

> dinomem-neuron install instructions are in the private repo after access is granted.


---

## License

MIT

---

Made with 🦖 by [@02-dino](https://github.com/02-dino)
