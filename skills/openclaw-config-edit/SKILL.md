---
name: openclaw-config-edit
description: Safely edit openclaw.json (gateway config) and roll shared extension/engine changes across multiple gateways: validate schema before restart to avoid crash-loops, canary one service before the fleet. Read this before any openclaw.json change — adding tools, plugins, agent fields, nonOwner allowlists, channels — before restarting more than one gateway, and when a bootstrap file is near its character cap, before trimming any content.
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

## Rolling a shared extension/engine across the fleet
`config validate` only proves the CONFIG parses. It says nothing about whether the gateway's Node loader will accept a changed extension file (`extensions/<name>/index.ts`). A loader that chokes makes the extension fail to load ENTIRELY — a security extension then yields no verdicts at all, which is open, not partially closed. Validate-then-restart-everything therefore converts a one-service problem into a fleet-wide outage.

1. **Canary one service first.** Restart the single least-critical profile, then grep its log for load errors before touching anything else. A loader surprise costs one gateway, not ten.
2. **Then the rest in small batches**, not one wide command.
3. **After ANY interrupted batch, measure before continuing.** A long `systemctl restart a b c` can be cut off mid-flight (SIGTERM / tool timeout), leaving some services down and ungated. Enumerate the real state — `is-active` plus the running version per profile — instead of assuming the command finished.
4. **A stale profile is not automatically a risk.** Compare what actually changed: a diff that is pure visibility (added `export`, tests) is zero-behaviour and can wait for the next natural restart. Decide from the diff, not from the version string being behind.
5. **Patching a live file is not a fix if an installer owns it.** When `install.sh` re-injects that file, the next install silently reverts you. `diff -q <live> <repo-source>`; if identical, the repo is the only durable place to change it.

## Safety net (optional, recommended): config-guard
`dinomem/features/config-guard/` installs an independent systemd watchdog that auto-reverts openclaw.json to the last known-good snapshot on a bad write. The shipped default reverts SYNTAX breakage only; it can also be set to revert SCHEMA breakage (valid JSON / bad key) on single-agent gateways via `GUARD_SCHEMA_RESTORE=1`. It is ENFORCEMENT, not a substitute for step 2 — still validate before restart. Install: `bash features/config-guard/install.sh --openclaw-dir <config-home>`.

## Context pressure: raise the ceiling before you cut content

A bootstrap file reported near its cap is NOT automatically proof the file is
bloated. Two different things produce the same symptom, and they have opposite
fixes:

- the **ceiling** is too low -> raise `bootstrapMaxChars` / `bootstrapTotalMaxChars`
- the **content** is genuinely redundant -> trim

Check the ceiling FIRST, because it is the reversible one. Trimming is lossy and
you cannot tell from a percentage which lines were worth keeping.

```bash
# current ceiling (per-agent home, not the default one)
python3 -c "import json;d=json.load(open('<config-home>/openclaw.json'))['agents']['defaults'];print(d.get('bootstrapMaxChars'), d.get('bootstrapTotalMaxChars'))"
# actual demand
wc -c IDENTITY.md SOUL.md TOOLS.md AGENTS.md
```

If demand is under the model's real context budget, raising the ceiling costs
nothing and loses nothing. Both fields are per-agent overrides, so confirm them
against `openclaw config schema` and write the agent's OWN config home.

### Then: measure what is dead, do not eyeball what is long

If a trim is still wanted, the owner's constraint is usually "do not degrade
quality". So cut only what is PROVEN redundant. Three probes, cheapest first —
each returns a number, and an empty result means there is nothing safe to cut:

1. **dead entries** — documented tools with no file on disk
2. **cross-file duplication** — identical lines in two bootstrap files
3. **intra-file duplication** — identical lines repeated inside one file

All three empty = the file is dense, not bloated. Say so and stop; trimming
anyway to show effort is a loss with no gain. Report the measured verdict.

### The tool may refuse: that refusal is a guardrail, not an obstacle

A patch to a protected config path can be rejected by design, so an agent cannot
quietly widen its own limits. Do not work around it. Take the validated CLI path
(`openclaw config patch/set` + `openclaw config validate`), back up the config
first, and re-read the value afterwards to prove it was actually applied —
a limit you did not re-read is a limit you are guessing about.
