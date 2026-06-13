---
name: dinomem
description: "Persistent memory system for OpenClaw agents — session archiving, LLM memory extraction, and local vector DB via TEI."
author: dino
---

# dinomem

Gives an OpenClaw agent long-term memory that survives session resets. Installs session archiving, memory extraction pipeline, and TEI embedding server.

## Install

```bash
git clone https://github.com/02-dino/dinomem
bash dinomem/scripts/install.sh --workspace <workspace> --agent-id <id>
openclaw gateway restart
```

## Verify

```bash
curl http://localhost:8080/health   # → {"status":"ok"}
crontab -l | grep auto_session_reset
```
