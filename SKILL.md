---
name: dinomem
description: Persistent agent memory system for OpenClaw. Installs auto session reset, memory extraction pipeline, and TEI vector DB. Use when setting up a new OpenClaw agent that needs long-term memory, session archiving, or context continuity across resets.
version: 0.1.0
lastUpdated: 2026-06-13
metadata:
  openclaw:
    emoji: "🦕"
    requires:
      bins: ["docker", "python3", "curl"]
triggers:
  - "install memory system"
  - "set up dinomem"
  - "agent needs long-term memory"
  - "set up session archiving"
  - "install vector db memory"
author: dinotlgrm
---

# 🦕 dinomem — Dino Agent Memory

Persistent memory system for OpenClaw agents. Gives your agent long-term memory that survives session resets — via automatic session archiving, memory extraction, and a local vector DB.

## When to use
- Setting up a new OpenClaw agent that needs memory continuity
- Agent keeps forgetting context after daily resets
- You want semantic search over past conversations
- You need session archiving + memory extraction pipeline

## Procedure

1. Clone the repo: `git clone https://github.com/dinotlgrm/dinomem`
2. Run install: `bash dinomem/scripts/install.sh --workspace <your-workspace-path> --agent-id <your-agent-id>`
3. Restart OpenClaw: `openclaw gateway restart`
4. Verify TEI is running: `curl http://localhost:8080/health`
5. Run first extraction: `python3 <workspace>/procedures/auto_session_reset.py`

The installer handles:
- Copying all scripts to your workspace
- Starting TEI embedding server via Docker
- Registering cron jobs (session reset every 15 min)
- Patching `openclaw.json` (session.reset idle 7d, compaction safeguard)
- Wiring `AGENTS.md` with memory search instructions

## Verification
```bash
curl http://localhost:8080/health
# → {"status":"ok"}

crontab -l | grep auto_session_reset
# → */15 * * * * cd <workspace> && python3 procedures/auto_session_reset.py ...
```

## References
- `references/architecture.md` — how the memory pipeline works
