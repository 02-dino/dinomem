# git-autosnapshot

Automatic, local git **changelog** of your OpenClaw agent workspace — `git log` reads as a why-history of what changed and when, that runs itself.

Every N minutes it commits changes to your agent's own workspace files (edits **and** brand-new files) **plus a secret-masked, agent-sliced copy of `openclaw.json`** so config drift is on the record too. Local-only by default: no remote, nothing leaves the box.

**This is a changelog, not a backup/recovery system.** dinomem already ships that: [`procedures/workspace_backup.py`](../../procedures/workspace_backup.py) (periodic full-workspace tar.gz snapshots, keep-N, restore CLI) is the supported way to recover a lost/broken file. git-autosnapshot exists to answer *"what changed, and why"* — not *"disaster struck, get my files back"*. See [`skills/backup-restore/SKILL.md`](../../skills/backup-restore/SKILL.md) for the full recovery order. Byte-exact recovery of an individual tracked file (`git show <sha>:<path>`) is a natural *side effect* of real git history and still works — it's just not the primary purpose.

## What you get

- **Changelog commit on a timer** (systemd timer, or cron fallback). Default every 15 min.
- **Curated scope, not the whole box** — one isolated `.dinomem-snap.git` git-dir *per workspace*, addressed via `--git-dir`/`--work-tree` so your own repo (if any) is never touched. The existing ignore rules already exclude vector DBs, sessions, logs, caches, sqlite, models — so the store tracks source-ish text (memory, config, skills, hooks, procedures) and stays small by construction.
- **Simple size guard** (default 10 MB) — an oversized new/grown file is just skipped that tick (stays on disk, untracked). No LFS, no allowlist matrix: the point of a curated text scope is that a legitimately huge file shouldn't be in this store's path at all.
- **Full permanent history** — no retention/collapse. Because scope stays small by construction (curated text/config only), keeping every tick forever is cheap; `git log` is a complete record, not a rolling window.
- **Scale config** — enables `core.fsmonitor`, `core.untrackedcache`, `feature.manyFiles` so staging stays fast.
- **Two-tier commit subjects** — the actual point of this feature. `git log --oneline` reads as a why-changelog instead of noise:
  - **Semantic** (Tier 1) — when a **meaningful** memory write happens (a pattern graduates/demotes, a fact is superseded, a done-note resolves, a cross-head dedup-merge), the caller hands the already-computed reason to the changelog writer, producing subjects like `promote: graduate "…" (3 reinforce, conf 0.82)`, `supersede: dino.location old → new`, `resolve: note <slug> done_when met @<sha>`, `dedup-merge: <peer> ← world-fact`.
  - **Structural** (Tier 2) — a blind timer tick genuinely has no *why*, so it keeps the machine-scannable fallback `auto-snapshot <ts> · +A ~M -D · <topdir> (N file(s))`.
  - **Zero new cost:** the reason is an f-string over values the caller already held — no LLM, no model call per tick. The mechanism is a fail-open reason-hint file (`.dinomem-commit-reason`) that the writer reads-then-clears; if it's absent/stale, the structural subject is used. Callers stay entirely git-free (one writer, `procedures/commit_reason.py` on the caller side).

## Install

```bash
bash features/git-autosnapshot/install.sh --repo ~/.openclaw
```

Options:

| Flag | Default | Meaning |
|---|---|---|
| `--repo DIR` | `$OPENCLAW_HOME` or `~/.openclaw` | workspace to changelog |
| `--interval-min N` | `15` | tick interval |
| `--max-mb N` | `10` | per-file ceiling for auto-added NEW/grown files — no LFS, oversized files are just skipped that tick |
| `--config-path PATH` | `$OPENCLAW_DIR/openclaw.json` (parent-of-`--repo`) | the openclaw.json that governs this workspace's agent. Default fits the standard single-gateway topology; override for a non-standard layout (e.g. a dedicated per-agent gateway instance) |
| `--config-snapshot-agent ID` | | slice + redact `--config-path` to ONLY this agent's own config block before tracking it (see "Agent-scoped config changelog" below) |
| `--force` | | overwrite existing units/scripts |
| `--dry-run` | | preview only, write nothing |
| `--uninstall` | | remove timer/cron (keeps commits, scripts, ignore rules) |
| `--all-workspaces` | | install one isolated store PER `workspace-*` dir under `--repo`, auto-deriving each one's `--config-snapshot-agent` from its dir name |
| `--include-only <glob>` (repeatable) | | scope the changelog to only these repo-relative pathspecs (used by the root-level store for `agents/**`/`shared/**`) |

## Agent-scoped config changelog (secret-masked, agent-sliced)

`openclaw.json` is the highest-value file to keep change-history on — one bad comma crash-loops the gateway — but it's dense with live secrets (`apiKey`, `botToken`, gateway auth token, `env` creds), and on many boxes it's **shared across multiple agents** in one file. Neither of those is safe to commit raw into a single agent's changelog: secrets would enter git history, and a global file would leak every other agent's config edits into this one agent's log.

`--config-snapshot-agent <agent-id>` solves both: each tick, [`config-redact.sh`](config-redact.sh) first **slices** the live config down to just that agent's own block (`.agents.list[]` entry matching the id — or `.agents.defaults`/the whole `.agents` object when there's no list entry, e.g. a dedicated single-agent file or the default/root agent), then **redacts** secret values, then writes the result to `configs/openclaw.json` inside the workspace's own store.

```bash
bash features/git-autosnapshot/install.sh --repo ~/.openclaw/workspace-analyst \
  --config-snapshot-agent analyst
```

This is the convenience form of the more general `--config-snapshot <src:agent:dst>` (repeatable; also accepts the older whole-file `<src:dst>` shape for cross-agent stores like the root `agents/**`+`shared/**` one, where slicing to a single agent wouldn't make sense).

- **src** = the live config (may live outside the workspace — e.g. a sibling gateway instance's `openclaw.json`).
- **agent** = the agent id to slice to (`.agents.list[].id` match).
- **dst** = a repo-relative path the store tracks (created if missing).
- Redaction masks by **key name** (case-insensitive substring: `apikey`/`token`/`secret`/`password`/`credential`/…), recursive, arrays included. Numeric tuning knobs that merely contain "token" (`maxTokens`, `keepRecentTokens`, …) are explicitly **not** masked.
- Only that agent's **own** `.agents.list[]` entry is tracked — NOT `.agents.defaults`. A defaults-level change (affecting every agent under that file) shows up in the root-level cross-agent store instead, so it isn't duplicated into every agent's log.
- **Fail-open + fail-safe:** invalid/half-written src is skipped (never overwrites a good redacted snapshot); a missing redactor, `jq`-less host, or unmatched agent id just skips config snapshotting for that tick; identical redacted output isn't rewritten (no phantom churn on a static config).
- **Restore** is structural only — you recover the config *shape/values* minus secrets. Re-inject secrets from your keystore.

## Honest limits

- **Local-only = no durability.** A changelog protects against *"what changed and why did I lose track"*, not disk failure. If the disk dies, history dies with it. For durability, add a remote you push to.
- **Not a disaster-recovery system.** Use `procedures/workspace_backup.py` for that — see `skills/backup-restore/SKILL.md` for the full recovery order.
- **No retention window.** Full history is kept forever; this is a feature (complete record), but it does mean the store grows monotonically over the life of the workspace — bounded by how much text/config actually changes, which for a curated scope is small.

## Uninstall

```bash
bash features/git-autosnapshot/install.sh --repo ~/.openclaw --uninstall
```

Removes the timer/cron only. Your commits, the installed scripts, and ignore rules stay put.

## Logs

`<repo>/logs/git-autosnapshot.log` — recovery events (stale lock removal), timestamped.
