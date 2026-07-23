#!/usr/bin/env python3
"""Single source of truth for dinomem's cheap / non-reasoning model.

AUTO-LINKED to your compaction model, so ONE anchor controls everything:
change `agents.defaults.compaction.model` in openclaw.json and every
non-reasoning dinomem LLM call (extraction, review, ...) follows automatically.
No second place to edit.

Precedence (first non-empty wins):
  1. DINOMEM_CHEAP_MODEL env      -> explicit override (a cron pinning a model)
  2. agents.defaults.compaction.model in openclaw.json  -> THE ANCHOR
  3. DINOMEM_NO_REASONING_MODEL   -> legacy back-compat env
  4. "" (empty)                   -> caller falls back to OpenClaw default

WHY empty and not a hardcoded model: dinomem ships to OTHER users on unknown
providers. A hardcoded fallback model id could 404 on their gateway. Returning
"" lets each caller degrade safely to the user's OpenClaw default (always valid).
The installer WARNS if this resolves empty so the user knows to set a cheap model.

Reasoning tasks stay on agents.defaults.model.primary; that is NOT this module's
job. This only resolves the cheap (non-reasoning) tier.

Portable: reads the current user's ~/.openclaw/openclaw.json (or $OPENCLAW_CONFIG).
No machine-specific paths.
"""
import json
import os
from pathlib import Path


def _config_path() -> str:
    env = os.environ.get("OPENCLAW_CONFIG", "").strip()
    if env:
        return env
    return str(Path.home() / ".openclaw" / "openclaw.json")


def _compaction_model(config_path: str = "") -> str:
    """Read agents.defaults.compaction.model. Empty string on any failure."""
    path = config_path or _config_path()
    try:
        with open(path, "r") as fh:
            cfg = json.load(fh)
        m = (
            cfg.get("agents", {})
            .get("defaults", {})
            .get("compaction", {})
            .get("model", "")
        )
        return (m or "").strip()
    except Exception:
        return ""


def cheap_model(config_path: str = "") -> str:
    """Resolve the cheap model.

    env override -> compaction anchor -> legacy env -> "" (caller uses default).
    """
    env = os.environ.get("DINOMEM_CHEAP_MODEL", "").strip()
    if env:
        return env
    anchor = _compaction_model(config_path)
    if anchor:
        return anchor
    return os.environ.get("DINOMEM_NO_REASONING_MODEL", "").strip()


if __name__ == "__main__":
    print(cheap_model())
