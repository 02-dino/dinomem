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
