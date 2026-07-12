---
name: dinomem-open-notes
description: "On every agent bootstrap, deterministically inject a blocking manifest of OPEN dinomem notes (status in_progress|pending) so the model can't miss unfinished work — replaces the old 'glob memory/_note_*.md before answering' AGENTS.md rule with a mechanism instead of a plea."
metadata:
  { "openclaw": { "emoji": "📌", "events": ["agent:bootstrap"], "requires": { "bins": [] } } }
---
# dinomem-open-notes

Zero-token-waste, deterministic "resume your open work" injection at bootstrap.

## Why

dinomem's project/task notes (`memory/_note_*.md`) carry a `status:` and a
`resume_state:`. The old contract was an AGENTS.md instruction: *"on the first
turn, glob `memory/_note_*.md` and read the open ones before answering."* That is
a **plea to the model** — the dinomem docs themselves flagged skipping this glob
as the single most common silent failure (open note one `ls` away that the model
never read, so it re-nags or restarts finished work).

This hook removes the glob-skip failure class entirely: it runs the glob itself,
**every bootstrap**, and injects a short **blocking manifest** of only the OPEN
notes directly into the model's bootstrap context. The model no longer has to
*remember to look* — the open-work list is already in front of it, phrased as a
must-read-before-answering directive.

## Design (why it's cheap AND reliable)

It is a **pointer manifest, not a full dump** — near-zero tokens:

- Lists only notes with `status: in_progress` or `status: pending` (finished /
  malformed notes are skipped).
- One line per note: **path · title · status · one-line `done_when`**.
- Sorted most-recent first (mtime), hard-capped (`DINOMEM_OPEN_NOTES_MAX`,
  default 5); any overflow is summarized as `+N more`.
- Rendered as an **imperative block** (`You MUST read these before your first
  answer …`) — not an FYI. Obeying it is one cheap `read` call, and the
  per-note `done_when` line makes relevance to the user's message obvious, so the
  read is self-triggering.

The deterministic win: the manifest **always appears** when open notes exist, so
"the model forgot to glob" can no longer happen. The only residual is that the
model still issues the follow-up `read` for a note it deems relevant — but that
is now a clearly-mandated, single cheap call rather than a fuzzy recall decision.

## What it does

On `agent:bootstrap`:

1. Resolves the workspace dir from the event context (`context.workspaceDir`,
   falling back to `context.cfg.workspace.dir`, then `OPENCLAW_WORKSPACE` /
   `DINOMEM_WORKSPACE`). Fully portable — no hardcoded paths.
2. Globs `<workspace>/memory/_note_*.md`, parses each note's `status:`,
   `# Title` (first heading), and `done_when:` from the frontmatter-ish header.
3. Keeps only open notes, sorts by mtime desc, caps at N, builds the manifest.
4. Pushes a single bootstrap entry (`name: "AGENTS.md"` so it survives the
   subagent/cron session filter, `content:` = the manifest) onto
   `context.bootstrapFiles`. If there are no open notes, it injects nothing
   (zero overhead on a clean workspace).

The handler is synchronous and fast (a directory read + small header parse); it
never blocks — on any error it logs and returns, leaving bootstrap untouched.

## Requirements

- No external binaries. Pure Node (`fs`), reads only `memory/_note_*.md`.
- A dinomem install in the target workspace (harmless if none: no notes = no-op).

## Config (optional)

| Env / config | Default | Effect |
| ------------ | ------- | ------ |
| `DINOMEM_OPEN_NOTES_MAX` | `5` | Max open notes injected in full; rest become `+N more`. |

## Enable

```bash
openclaw hooks enable dinomem-open-notes
openclaw gateway restart
```

## Relationship to the AGENTS.md block

This hook **replaces** the `M_session_start` glob rule that used to live in the
injected AGENTS.md block. After enabling it, that rule is redundant in AGENTS.md
(dinomem's installer ships the shortened block accordingly). The remaining
recall-discipline rules (M0–M3, `investigate_before_act`) stay inline — those are
model-reasoning directives a hook cannot mechanize.
