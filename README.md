# 🦕 dinomem — Dino Agent Memory

> Your OpenClaw agent forgets everything when a session resets. dinomem fixes that.

Every decision you've made, every preference you've set, every pattern your agent has learned — gone the moment a session resets. dinomem gives your agent a memory that persists: sessions are archived, key facts are extracted by an LLM, and everything is embedded locally for semantic search. Your agent picks up exactly where it left off.

---

## What it does

- **Auto session archiving** — old sessions are archived automatically before they're lost. Nothing gets dropped silently.
- **Memory extraction** — an LLM reads archived sessions and distills key facts, decisions, preferences, patterns, and lessons into `memory/*.md`
- **Semantic search** — memories are embedded locally (no API calls, no cloud) and searchable via `memory_search`
- **Memory pinning** — tell your agent "remember this" and it saves a permanent `_pin_*.md`, protected from all cleanup. For todos and reminders, `_note_*.md` — auto-deleted once resolved.
- **Memory cleanup** — daily dedup + weekly LLM review keeps memory lean. Noise removed, contradictions flagged.
- **Agent self-configuration** — tell your agent to change its tone, add a tool, or set a rule — it writes to the right file automatically
- **Zero-config install** — one script handles Docker, cron, and OpenClaw config patches

---

## How memory works

```
OpenClaw session (.jsonl)
        │
        │  every 15 min (cron)
        ▼
[session_reset.py]
  Archives sessions older than 7 days or after 5 compactions
        │
        ▼
[extract_memory.py]
  LLM reads archived sessions → extracts facts, decisions, preferences, patterns, lessons
  Writes to memory/YYYY-MM-DD.md + updates MEMORY.md index
        │
        ▼
[TEI embedding server]
  Embeds MEMORY.md entries locally (sentence-transformers, ~80MB, CPU-only)
        │
        ▼
[memory_search tool]
  Agent queries past memories semantically on every relevant request
```

**Session reset triggers** (any one condition):

| Condition | Default |
|-----------|---------|
| Session age (chat) | > 7 days idle |
| Session age (cron/isolated) | > 1 day |
| Compaction count | > 5 |
| Orphaned file age | > 48 hours |

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

## Using dinomem

### Memory pinning

Tell your agent to remember something permanently:

> "Remember this: my wife's birthday is June 23"

The agent saves it as `memory/_pin_<slug>.md` — protected from all cleanup scripts, never auto-deleted. Only recalled when relevant — e.g. when you ask "when is my wife's birthday?" or "what's coming up in June?". Not injected every turn.

For things you want to build or do:

> "Remember to add dark mode to the app"

Saved as `memory/_note_<slug>.md`. Recalled when you ask "what's on my build list?". Auto-deleted by daily cron once the agent detects it's been done.

> **Note:** Memory is recall-based, not always-on. The agent searches for relevant memories when needed — nothing is automatically injected into every turn.

> ⚠️ Memory is for **short, recallable knowledge** — facts, decisions, preferences, patterns, lessons, and user traits. Do not save long documents (contracts, books, legal text) to memory — large files pollute LLM context and degrade agent behavior.
>
> **Want this?** dinomem-neuron is a paid upgrade to dinomem that adds full RAG support for long documents — contracts, books, legal text — stored separately and searchable without polluting memory.

> dinomem-neuron also takes `_note_` further — if your note implies a date or deadline, it automatically creates a Google Calendar event and deletes the note when the date passes.


### Agent self-configuration

Not sure where to put something? Just tell your agent:

> "Be more concise"
> "Your name is Aria"
> "Always check X before doing Y"
> "I built a script that does Z, add it as a tool"

dinomem includes a routing system that detects your intent and writes to the correct file automatically — `SOUL.md` for tone, `IDENTITY.md` for persona, `AGENTS.md` for rules and workflows, `TOOLS.md` for tools, `USER.md` for your preferences. Backs up before every write.

---

## Want more?

dinomem is the foundation. After a few weeks, your agent has hundreds of memories.
**dinomem-neuron** is what happens next.

> *"After 3 weeks, my agent told me I've been contradicting myself on position sizing — I never noticed."*
>
> *"It connected a decision I made in January to a pattern from November. I didn't remember either."*
>
> *"My agent now has a structured knowledge base it built itself. I didn't write a single line of it."*

It also adds:
- **Calendar integration** — `_note_` files linked to Google Calendar, auto-deleted when the event passes
- **RAG for long docs** — semantic search over contracts, books, legal text without touching memory
- **Contradiction detection** — flags when new memories conflict with existing ones
- **Emergent insights** — finds patterns across memories you never explicitly stated

The repo is private — access granted after onboarding.

Interested? → [@dinotlgrm](https://t.me/dinotlgrm) on Telegram

---

## Install options

| Flag | Default | Description |
|------|---------|-------------|
| `--workspace DIR` | `$OPENCLAW_WORKSPACE` or `~/.openclaw/workspace` | Path to agent workspace |
| `--agent-id ID` | Detected from workspace name | OpenClaw agent ID |
| `--no-docker` | — | Skip TEI Docker setup |
| `--no-cron` | — | Skip crontab registration |
| `--force` | — | Overwrite existing scripts |

---

## What gets installed

```
<workspace>/
├── procedures/
│   ├── auto_session_reset.py   # Cron entry point — runs every 15 min
│   ├── session_reset.py        # Archives old/compacted sessions
│   └── extract_memory.py       # Extracts memories from archives via LLM
├── tools/
│   ├── memory_cleanup.py       # Daily dedup of memory files
│   └── memory_review.py        # Weekly LLM review (valid/invalidated/noise)
├── logs/
└── memory/
    ├── _pin_*.md               # Permanent user-pinned memories (never deleted)
    ├── _note_*.md              # Transient todos/reminders (auto-deleted when resolved)
    └── YYYY-MM-DD.md           # Daily memory files (auto-generated)
MEMORY.md                       # Searchable index (auto-generated, do not edit)
```

---

## OpenClaw config patches

The installer automatically patches `~/.openclaw/openclaw.json`:

| Config | Value | Reason |
|--------|-------|--------|
| `session.reset.mode` | `idle` | Prevent premature daily resets |
| `session.reset.idleMinutes` | `10080` | Reset only after 7 days of inactivity |
| `contextPruning.mode` | `off` | Compaction summarizes — TTL pruning just drops |
| `compaction.mode` | `safeguard` | Summarizes before dropping context |
| `compaction.memoryFlush.enabled` | `false` | **Must stay disabled** — memoryFlush triggers its own session reset which clashes with `auto_session_reset.py` |
| `memorySearch.provider` | `openai-compatible` | Use local TEI server |
| `memorySearch.remote.baseUrl` | `http://localhost:8080/v1` | TEI Docker endpoint |
| `agents.defaults.workspaceBootstrap` | `always` | Root files (AGENTS.md, SOUL.md, etc) injected every turn — not skipped on continuation turns |

See `references/openclaw-config-snippet.json5` for the full annotated config.

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

**Does it work without Docker?**
TEI requires Docker. Without it, `memory_search` falls back to OpenClaw's built-in search (less accurate). Use `--no-docker` to skip TEI setup and configure a remote embedding server manually.

**How much disk space does it use?**
TEI model: ~80MB. Memory files: minimal (text only). Vector DB grows with usage — roughly 1–2MB per 1000 memory entries.

**Does it work on Windows?**
Not natively. Use WSL2 with Ubuntu.

**Will it affect my existing agent config?**
The installer patches `openclaw.json` and appends to `AGENTS.md`. It does not delete anything. Use `--force` only to overwrite existing scripts.

**What LLM does it use for memory extraction?**
Your OpenClaw default model via the gateway. Falls back to OpenRouter (`google/gemini-2.5-flash`) if the gateway call fails.

**How is this different from OpenClaw's built-in memory?**
OpenClaw has native `memory_search`/`memory_get` tools but no automatic extraction pipeline. dinomem adds the pipeline: session archiving → LLM extraction → structured `memory/*.md` files → embeddings. The native tools then search those files.

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
- `--purge-data` — also delete `memory/`, `logs/`, `MEMORY.md` (asks for confirmation)

Run `openclaw gateway restart` after uninstall to apply config changes.

---

## License

MIT

---

Made with 🦖 by [@02-dino](https://github.com/02-dino) | [komunitech.com](https://komunitech.com)
