# rea

Wires the **REA** reverse-engineering capability into an OpenClaw agent.

## Why dinomem ships it

dinomem gives an agent **memory**. REA gives it the ability to **measure an
unknown artifact** — a site, an APK, a binary, an Electron/ASAR bundle, a
minified JS blob — instead of guessing from its name.

Those compose: *"what did we learn about X"* is only as good as the agent's
ability to look at X in the first place. Wiring it here means one install
instead of two.

## What this is (and is not)

A **shim**, not a fork. It fetches
[02-dino/rea-installer](https://github.com/02-dino/rea-installer) and delegates
to it, always with `--harness openclaw`. That upstream installer also supports
`claude | cursor | codex | generic`; dinomem is an OpenClaw plugin, so the
OpenClaw path is pinned.

Keeping it a shim means REA upgrades land upstream instead of rotting in a copy
here.

Upstream installs: the REA CLI, Ghidra (free native decompiler), JDK 21+ if
missing, plus a `SKILL.md` and an intent-based trigger block in `AGENTS.md`.
Deliberately **not** installed: IDA Pro, Hopper (commercial), JADX (needs a
bring-your-own MCP JAR). No MCP server is registered — REA runs as a plain CLI.

## Install

Default-ON during `scripts/install.sh`. Opt out with `--no-rea`.

Standalone:

```bash
bash features/rea/install.sh [--workspace DIR] [--no-ghidra] [--force] [--dry-run]
```

| flag | meaning |
|---|---|
| `--workspace DIR` | workspace whose `AGENTS.md` gets the REA trigger block |
| `--no-ghidra` | skip the decompiler (web/JS/APK-parse still work) |
| `--force` | refresh `SKILL.md` + trigger block even if present |
| `--dry-run` | preview, write nothing |

## Failure behaviour

**Network-dependent and strictly non-fatal.** Missing `git`, an offline box, an
unreachable GitHub, or a non-zero upstream exit each log a warning and let the
dinomem install continue. An optional capability must never block a memory
install.

The clone goes to a private `mktemp -d` removed on exit, so a half-finished
fetch can never be mistaken for a good one.

## Verify

```bash
rea --help
command -v rea
```
