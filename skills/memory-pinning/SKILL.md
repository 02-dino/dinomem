---
name: memory-pinning
description: Create durable dinomem memory — permanent _pin_ facts and transient _note_ todos/reminders/projects. Read this when the user says remember/pin/note this, when you commit to deferred work, or when a task/reminder/time-bound item arises and you need the exact file format, slug rules, and done_when discipline.
---

# Memory pinning (dinomem)

dinomem persists two kinds of durable memory as files in `memory/`. Use this
skill to write them correctly so the resolver crons and `memory_search` can find
and garbage-collect them properly.

## When to use

- User says "remember this", "pin this", "note that", "don't forget" (any phrasing).
- Your own reply commits to deferred/external work not finished this turn.
- A todo, reminder, planned task, or time-bound item arises.
- A multi-step build begins (→ use a **project** note, see below).

If it's a fleeting/ambiguous personal detail, **ask before pinning**.

## Two kinds

### 1. Permanent facts → `_pin_`

For durable facts, preferences, decisions — things true across sessions.

- **Trigger:** a permanent fact, or the user emphasizes importance. Uncertain? ask first.
- **File:** `memory/_pin_<slug>.md`
- **Slug:** lowercase-hyphens, max ~30 chars.
- **Format:**
  ```
  # Title

  <content>
  ```
- **Long documents** (legal text, manuals, contracts): don't inline — ingest via
  `docs/<slug>.md` → `docs_ingest.py` instead.

### 2. Transient work → `_note_`

For todos, reminders, planned tasks, time-bound items.

- **Trigger:** a todo / reminder / planned task / time-bound item. Uncertain? ask first.
- **File:** `memory/_note_<slug>.md` (slug: lowercase-hyphens, max ~30).
- **Format:**
  ```
  # Title
  type: task_bound | time_bound
  status: pending | done
  date: YYYY-MM-DD
  done_when: <checkable — file exists / feature shipped>
  stale_after: YYYY-MM-DD
  <content>
  ```

**Schema rules:**

| Field | Meaning |
| ----- | ------- |
| `type` | `task_bound` resolves via `done_when`; `time_bound` is a date-based reminder. |
| `done_when` | Concrete artifact check. This is the lever the resolver cron uses to flip `status: done` and delete the note (task_bound only). |
| `stale_after` | Fallback GC for abandoned notes. Default `date + 30d` (reminders `date + 7d`); you may override. |
| `status` | Flip to `done` **only** when `done_when` is verified; otherwise `pending`. |

**`done_when` MUST be locally verifiable.** The resolver runs against **local
state only** — no chat history, no network identity. Good: `file exists`,
`grep`, `exit 0`. For a git push: `git -C <repo> rev-parse HEAD == @{u}`.
**Never** write narrative done_when like "pushed to repo" or "told the user" —
those are unverifiable, so the note never auto-closes and rots. If a condition
isn't locally checkable, don't rely on auto-close.

The resolver only acts on `done_when` + `stale_after`; any other fields you add
to a note are left untouched.

## Projects (multi-step builds) → `_note_` with `type: project`

When a request needs more than a couple of sequential steps (multi-file build,
research-then-build), write a **project note** so the plan survives context
limits and new sessions:

```
# Project: <name>
type: project
status: in_progress | done
date: YYYY-MM-DD
done_when: <final artifact / all steps [x]>
stale_after: YYYY-MM-DD
current_step: <n>
steps:
  - [ ] 1. <step> -- done_when: <check>
  - [ ] 2. ...
resume_state: <paths/decisions/assumptions the next turn needs to resume blind>
```

After **every** step, rewrite `resume_state` so the note is self-sufficient — a
fresh session (or the open-notes bootstrap hook) can jump straight to
`current_step` and resume with no reliance on chat history. This is the
reliability linchpin.

## Dedup before creating

Before authoring a **new** `_pin_` or `_note_`, check whether an existing file
already covers the topic (semantic, not just slug match). If one does, **open and
update it** instead of stacking a second, possibly-contradictory file. If dedup
tooling isn't available, just proceed — never block a memory write on it.
