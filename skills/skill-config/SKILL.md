---
name: skill-config
description: Create, list, or remove an agent skill (on-demand procedural knowledge — a workflow, checklist, or how-to the agent reads only when a specific task appears) by routing intent to dinomem's skill_tool.py. Read this when the user wants the agent to learn a repeatable method/procedure that is needed sometimes, not every turn.
---

# Skill-config (dinomem)

Route here when the request is procedural knowledge the agent should read ON-DEMAND
for a specific task class — not always-on. Never hand-edit `openclaw.json`.

## Route first

Run `tools/route.py classify` and confirm the arbiter selected **skill** (discriminator 3).
If it runs on a schedule -> cron-config. If it reacts to a gateway event -> hook-config.
If it is an always-true rule/identity/preference -> self-config (root file).

## How

1. Read the routing map: open `tools/skill_tool.py` docstring — slug rules, trigger-vs-body split, confirm policy.
2. Generate `name`, `description` (the trigger surface: WHEN to read + WHAT it gives, <=1 sentence), and `body` (machine-readable steps).
3. Decide the trigger: description frontmatter is primary. Add `--trigger "<one line>"` ONLY if the description alone is too weak to fire reliably. Never inline the body into AGENTS.md.
4. Call `skill_tool.py scaffold <slug> --name .. --desc .. --body .. [--trigger ..]`.

## Confirm-before-write

Skills change agent capability. `scaffold` and `remove` require `--confirmed`. Confirm with the user first.
