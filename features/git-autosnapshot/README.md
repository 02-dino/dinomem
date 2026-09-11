# git-autosnapshot

Automatic, local git snapshots of your OpenClaw repo — a rollback-safety net that runs itself.

Every N minutes it commits all non-ignored changes (edits **and** brand-new files) so your work is always recoverable. Local-only by default: no remote, nothing leaves the box. It's a safety net on top of your real, hand-written commits — not a replacement for them.

## What you get

- **Auto-commit on a timer** (systemd timer, or cron fallback). Default every 15 min.
- **Captures new files too**, with an LFS-aware per-file size guard (default 10 MB). Oversized **non-LFS** blobs (e.g. a stray `.jsonl`/`.sqlite`/model dump) are refused from staging so they can never bloat `.git`. Oversized **LFS-tracked** files (media/archives/pdf) are exempt — see below.
- **git-lfs media handling** — images/video/pdf/fonts route through lfs so `.git` history stays small no matter how large the binary. Because the size guard is LFS-aware, a 40 MB `.mp4` is added *via* lfs (bytes stored outside history) instead of being dropped.
- **Disk-aware cleanup** — housekeeping escalates as the disk fills:

  | Disk used | Tier | Action |
  |---|---|---|
  | `<80%` | HEALTHY | light `gc --auto` + `lfs prune`, ~hourly |
  | `80–89%` | WARN | `gc --prune=now` + `lfs prune` + collapse old snapshots (>`RETAIN_DAYS`), every tick |
  | `≥90%` | EMERGENCY | reflog expire + aggressive `gc` + `lfs prune --force` + collapse old snapshots (>7d), every tick |

- **History retention** — old `auto-snapshot` commits collapse into a baseline when disk is tight, so 15-min snapshots (~35k/year) can't balloon `.git`. **Only `auto-snapshot` commits are ever collapsed** — your hand-written commits are permanent at any age. A backup ref is taken before any rewrite; a failed rewrite auto-restores.
- **Scale config** — enables `core.fsmonitor`, `core.untrackedcache`, `feature.manyFiles` so staging stays sub-second into six-figure file counts.
- **Two-tier commit subjects** — dinomem's git store is a **whole-workspace** changelog (memory writes *and* self-config / skill / hook / cron mutations), so the *why* of a change matters as much as the change. `git log --oneline` reads as a why-changelog instead of noise:
  - **Semantic** (Tier 1) — when a **meaningful** mutation happens, the caller hands the already-computed reason to the snapshot writer. Wired callers and the subjects they produce:
    - memory (`procedures/extract_memory.py`, `procedures/memory_review.py`): `memory: supersede <file> (update)`, `memory: merge detail into <file>`, `memory: graduate <file> (all-valid @ 90d)`
    - config (`tools/config_tool.py`): `config: patch <section> in AGENTS.md`, `config: append to TOOLS.md`, `config: rewrite IDENTITY.md`
    - skill (`tools/skill_tool.py`): `skill: scaffold <slug> (<name>)`, `skill: remove <slug>`
    - hook (`tools/hook_tool.py`): `hook: scaffold <name> for <event>`, `hook: remove <name>`
    - cron (`tools/cron_tool.py`): `cron: add <name> (tier=T2, cron)`, `cron: remove <name>`
  - **Multi-reason body** — two meaningful writes in one 15-min tick is common on a whole-workspace store, so each caller **appends** a line (de-duped, capped at `REASON_MAX_LINES`). The reader uses line 1 as the commit **subject** and lines 2+ as the commit **body** — so both whys survive, not just the last.
  - **Structural** (Tier 2) — a blind timer tick genuinely has no *why*, so it keeps the machine-scannable fallback `auto-snapshot <ts> · +A ~M -D · <topdir> (N file(s))`.
  - **Zero new cost, fail-open:** the reason is an f-string over values the caller already held — no LLM, no model call per tick. The mechanism is a reason-hint file (`.dinomem-commit-reason`) written by `procedures/commit_reason.py` (`drop()`; an optional thin `scripts/lib/commit_reason.sh` wrapper execs it for shell callers) that the writer reads-then-clears; if it's absent/stale, the structural subject is used. A hint is cosmetic — its write path swallows every error so it can never block/slow/crash the mutation it decorates. Callers stay entirely git-free (one writer).

## Install

```bash
bash features/git-autosnapshot/install.sh --repo ~/.openclaw
```

Options:

| Flag | Default | Meaning |
|---|---|---|
| `--repo DIR` | `$OPENCLAW_HOME` or `~/.openclaw` | repo to snapshot |
| `--interval-min N` | `15` | snapshot interval |
| `--max-mb N` | `10` | per-file ceiling for auto-added NEW files |
| `--retain-days N` | `30` | granular-history window before old snapshots collapse |
| `--no-lfs` | | skip git-lfs media tracking |
| `--force` | | overwrite existing units/scripts |
| `--dry-run` | | preview only, write nothing |
| `--uninstall` | | remove timer/cron (keeps commits, scripts, .gitignore) |

## Keeping an oversized *non-LFS* file on purpose

The size guard is deliberately strict about non-LFS blobs (a stray `.jsonl`/`.sqlite`/model dump). But sometimes you have an irreproducible, non-media blob you *do* want versioned. Opt it in with a `.dinomem-keep-large` file at the repo root — one glob per line, matched against the repo-relative path:

```
# .dinomem-keep-large — oversized NON-LFS blobs to version anyway
exports/*.sqlite
data/keep-*.jsonl
```

- Blank lines and `#` comments are ignored.
- Matched files skip the size exclusion and get committed as normal git blobs (they *do* count against `.git` size — that's the tradeoff you're opting into).
- Absent file = nothing allowlisted = default safe behavior.
- This is for **non-LFS** blobs only; media/archives/pdf are already handled by LFS and never need an entry.

## Snapshotting config (secret-masked)

`openclaw.json` is the highest-value file to keep undo-history on — one bad comma
crash-loops the gateway — but it's dense with live secrets (`apiKey`, `botToken`,
gateway auth token, `env` creds). Committing it raw every tick would turn the
local snapshot store into a time-machine of every key you've ever held.

Use `--config-snapshot <src>:<dst>` (repeatable) to capture a **secret-masked**
copy instead. Each tick, `config-redact.sh` reads the live config and writes a
structure-preserving copy to `<dst>` (repo-relative) with secret *values* masked
(`"***]"`) — keys, routes, models, tuning knobs stay intact so diffs remain
meaningful, but no credential ever enters git.

```bash
bash features/git-autosnapshot/install.sh --repo ~/.openclaw \
  --config-snapshot "$HOME/.openclaw/openclaw.json:configs/main.openclaw.json" \
  --config-snapshot "$HOME/.openclaw-sales/openclaw.json:configs/sales.openclaw.json"
```

- **src** = the live config (may live outside the repo — e.g. a sibling
  instance's `openclaw.json`).
- **dst** = a repo-relative path the snapshot store tracks (created if missing).
- Masking is by **key name** (case-insensitive substring: `apikey`/`token`/
  `secret`/`password`/`credential`/…), recursive, arrays included. Numeric
  tuning knobs that merely contain "token" (`maxTokens`, `keepRecentTokens`, …)
  are explicitly **not** masked, so their history stays useful.
- **Fail-open + fail-safe:** invalid/half-written src is skipped (never
  overwrites a good redacted snapshot); a missing redactor or `jq`-less host
  just skips config snapshotting; identical redacted output isn't rewritten
  (no phantom churn on a static config).
- **Restore** is structural only — you recover the config *shape/values* minus
  secrets. Re-inject secrets from your keystore, or keep the raw config in a
  separate secured backup if you need byte-exact restore.

## Honest limits

- **Local-only = no durability.** Snapshots protect against *mistakes*, not disk failure. If the disk dies, history dies with it. For durability, add a remote you push to (GitHub / self-hosted) — that also offloads lfs binaries off the local disk.
- Cleanup reclaims *git/lfs overhead* (old versions, loose objects, collapsible snapshots) — it **cannot** delete your actual files. If real data fills the disk toward 90%+, cleanup buys runway + a logged warning, not infinite space.
- Retention is **irreversible for collapsed snapshots**: you keep the files at every retained point, but lose the granular step-by-step diffs of snapshots older than the window.

## Uninstall

```bash
bash features/git-autosnapshot/install.sh --repo ~/.openclaw --uninstall
```

Removes the timer/cron only. Your commits, the installed scripts, and `.gitignore` stay put.

## Logs

`<repo>/logs/git-autosnapshot.log` — tier escalations and retention events, timestamped.
