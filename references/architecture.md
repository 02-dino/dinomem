# dinomem — Architecture

## Memory Pipeline

```
[OpenClaw session transcript]
        │
        │  every 15 min
        ▼
[auto_session_reset.py]  ← cron entry point
        │
        ├─► [session_reset.py]
        │       • Scans agents/<id>/sessions/sessions.json
        │       • Archives sessions older than threshold (default: 7 days chat, 1 day cron)
        │       • Triggers on compaction count > 5
        │       • Renames to .archived.reset.<timestamp>.jsonl
        │
        └─► [extract_memory.py]
                • Reads archived .jsonl files
                • Extracts key facts via LLM
                • Writes to memory/YYYY-MM-DD.md files
                • Indexes into MEMORY.md (searchable index)
                • Ingests into vector DB via TEI

With truncateAfterCompaction: true, OpenClaw also rotates the active session
file after each compaction — the pre-compaction transcript is left on disk
for session_reset.py to archive, while the active session continues from a
clean successor file (summary + unsummarized tail only).
```

## Vector DB (TEI)

dinomem uses [Text Embeddings Inference](https://github.com/huggingface/text-embeddings-inference) with `sentence-transformers/all-MiniLM-L6-v2`:

- Runs locally via Docker on port 8080
- OpenAI-compatible API (`/v1/embeddings`)
- ~80MB model, CPU-friendly, no GPU required
- OpenClaw memory search uses this via `memorySearch.remote.baseUrl`

## Session Reset Logic

`session_reset.py` resets a session when ANY of these conditions are met:

| Condition | Default threshold |
|-----------|------------------|
| Session age (chat) | > 7 days |
| Session age (cron/isolated) | > 1 day |
| Compaction count | > 5 |
| Orphaned file age | > 48 hours |

Grace period: sessions updated within the last 5 minutes are skipped to avoid interrupting active conversations.

## Memory Files

```
<workspace>/
└── memory/
    ├── 2026-06-01.md      # Daily memory file (auto-generated)
    ├── 2026-06-02.md
    ├── _pin_*.md          # Permanent user-pinned memories (never deleted)
    ├── _note_*.md         # Transient todos/reminders (auto-deleted when resolved)
    └── ...
MEMORY.md                  # Searchable index (auto-generated)
```

`MEMORY.md` is regenerated on every extraction run. Each line is a searchable entry:
```
TOPIC [Nd] Short description | tag1, tag2, tag3 | Detail sentence
```

## OpenClaw Config Requirements

dinomem requires these OpenClaw config values to work correctly.
The `install.sh` script patches all of these automatically.
See `references/openclaw-config-snippet.json5` for the full annotated reference.

| Config | Value | Reason |
|--------|-------|--------|
| `session.reset.mode` | `idle` | Prevent premature daily resets |
| `session.reset.idleMinutes` | `10080` (7 days) | Reset only after true inactivity |
| `contextPruning.mode` | `off` | Disable TTL-based blunt pruning — let compaction summarize instead |
| `compaction.mode` | `safeguard` | Smart summarization before dropping context |
| `compaction.truncateAfterCompaction` | `false` | Keep disabled — rotating session files resets `compactionCount`, breaking `session_reset.py`'s compaction threshold trigger |
| `compaction.memoryFlush.enabled` | `true` | Guarded bare-daily-file writer for `startupContext` (prompt confines it to `memory/YYYY-MM-DD.md`, forbids `MEMORY.md`). See README. |
| `agents.defaults.contextInjection` | `always` | Root files injected every turn, not skipped on continuation turns. Valid key is `contextInjection` (already the OpenClaw default); the invalid `workspaceBootstrap` key used by earlier versions crashed the gateway. |
| `memorySearch.provider` | `openai-compatible` | Use local TEI server |
| `memorySearch.remote.baseUrl` | `http://localhost:8080/v1` | TEI Docker endpoint |

> `reserveTokens` and `keepRecentTokens` are **not patched** — they are model-agnostic and vary by context window size. See `references/openclaw-config-snippet.json5` for recommended values per model type.

### Why contextPruning: off?

TTL-based pruning drops messages by age regardless of importance — a message from 61 minutes ago gets dropped even if it contains a critical decision. Compaction is smarter: it summarizes the oldest messages before dropping them, preserving signal. With `contextPruning: off`, compaction triggers when `contextTokens > contextWindow - reserveTokens` (e.g. 150k for a 200k window model).

### Agent tools.allow

Each agent must have `memory_search` and `memory_get` in their `tools.allow` list:

```json
{
  "agents": {
    "list": [{
      "id": "your-agent-id",
      "tools": {
        "allow": ["memory_search", "memory_get", "...other tools"]
      }
    }]
  }
}
```

The installer checks for this and warns if missing — but does not auto-patch it (agent tool lists are intentionally explicit).
