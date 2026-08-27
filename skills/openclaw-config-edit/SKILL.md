---
name: openclaw-config-edit
description: Safely edit openclaw.json (gateway config): validate schema before restart to avoid crash-loops. Read this before any openclaw.json change — adding tools, plugins, agent fields, nonOwner allowlists, channels.
---

# Editing openclaw.json (gateway config)

openclaw.json is the gateway rulebook. A bad write (schema-invalid OR syntax-broken) makes the gateway fail config load; with systemd `Restart=always` that becomes a crash-loop — every channel dies until the file is hand-fixed. `self-config`/`config_tool.py` does NOT cover openclaw.json (it only writes bootstrap .md). So there is no automatic schema gate on this path — you must run it yourself.

## Mandatory sequence (never skip step 2)
1. Edit openclaw.json (prefer `openclaw config patch/set/unset`; hand-edit only if needed).
2. VALIDATE: `OPENCLAW_CONFIG_PATH=<config-home>/openclaw.json openclaw config validate`
   - Must print `Config valid` with NO `×` errors. Warnings (e.g. stale plugin) are OK.
   - If any `×` → fix and re-validate. Do NOT restart on a × error.
3. Restart the target gateway service ONLY after validate passes
   (e.g. `systemctl --user restart openclaw-gateway-<profile>.service`, or your platform's gateway restart).
4. VERIFY health: `curl -s http://127.0.0.1:<port>/health` → expect `{"ok":true,"status":"live"}`.

## Placement rules (schema traps that pass JSON but fail schema)
- `nonOwnerAllowedTools` / `nonOwnerAllowedScripts` → ONLY inside `plugins.entries.<plugin>.config` (e.g. a dinotrust-enforce plugin). NEVER at `agents.list[]` level: `additionalProperties:false` rejects the WHOLE agent object → crash-loop.
- Per-agent overrides go in the agent's own valid fields or the relevant plugin config, not invented keys.
- Confirm a key is valid BEFORE writing: `openclaw config schema` (or the plugin's `openclaw.plugin.json` `configSchema`).

## Multiple gateways = multiple config homes
- Each gateway/profile has its own config home and port (main + any extra profiles). They are separate files.
- Always pass `OPENCLAW_CONFIG_PATH` for the RIGHT home when validating a non-default gateway, and restart the matching service.

## Safety net (optional, recommended): config-guard
`dinomem/features/config-guard/` installs an independent systemd watchdog that auto-reverts openclaw.json to the last known-good snapshot on a bad write. The shipped default reverts SYNTAX breakage only; it can also be set to revert SCHEMA breakage (valid JSON / bad key) on single-agent gateways via `GUARD_SCHEMA_RESTORE=1`. It is ENFORCEMENT, not a substitute for step 2 — still validate before restart. Install: `bash features/config-guard/install.sh --openclaw-dir <config-home>`.
