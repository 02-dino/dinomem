# worker-leak-guard

Cron janitor that trims **orphaned OpenClaw inference workers**.

## The leak

dinomem drives the OpenClaw CLI under a timeout. Python's
`subprocess.run(timeout=N)` kills only the **direct** child — but that child
(the `openclaw` CLI) spawns its own `openclaw-infer` worker holding 300–600MB.
On timeout the CLI dies and the worker survives, reparented to init (`ppid=1`),
with no caller left to read its output.

Measured on an 11-agent box (2026-10-19):

| | |
|---|---|
| orphan birth rate | ~2.3/min |
| alive at once | 22 workers (~8GB) |
| load average | 81 on 8 cores |
| knock-on | a gateway missed its shutdown deadline, got `SIGKILL`ed mid config write, and silently reverted an access-control change |

## Source fix vs. this guard

dinomem fixes the **source**: `procedures/extract_memory.py` now runs the CLI
in its own process group and kills the **group** on timeout, so no grandchild
can survive.

This guard is the **safety net** for the same bug class — any other leaking
path, a version skew, or a half-updated install. Same split as
[config-guard](../config-guard/README.md): the source fix is prevention, the
guard is enforcement.

## Why killing orphans is safe

`ppid=1` means the spawning process is **gone**, so nothing is waiting on this
worker's output — it is already discarded. Killing it cannot truncate a live
reply.

**A worker with a living parent is never touched**, at any age or size. That
is the entire safety argument; preserve it if you edit the matching logic.

The age floor (default 3 minutes) exists only to avoid racing a worker whose
parent died milliseconds ago. It is derived from the upstream 120s CLI
timeout: an orphan from that path is >2 minutes old by construction.

Matching is `pgrep -x` (exact process **name**), not `pgrep -f` — so a shell or
editor whose command line merely mentions `openclaw-infer` is never matched.

## Install

```bash
bash features/worker-leak-guard/install.sh
```

Registers one tagged cron line (`# dinomem-managed:worker-leak-guard`) running
every 5 minutes. Re-running **updates that line in place** — it never appends a
second copy and never touches untagged jobs.

### Options

| flag | default | meaning |
|---|---|---|
| `--min-age-min N` | `3` | orphan age floor, minutes |
| `--every-min N` | `5` | cron period, minutes |
| `--force` | off | overwrite an existing guard script |
| `--dry-run` | off | preview, write nothing |
| `--uninstall` | — | remove the cron line (leaves the script on disk) |

### Tuning the age floor

Worst-case backlog ≈ `birth_rate × (min_age + cron_period)`. At 2.3/min:

| `--min-age-min` | window | backlog | RAM held |
|---|---|---|---|
| 10 | 15m | ~34 | ~14GB |
| **3** (default) | 8m | ~18 | ~7GB |

Lower than 3 buys little and starts approaching the parent's death window.

## Verify

```bash
bash ~/.openclaw/scripts/worker-leak-guard.sh --dry-run   # report, kill nothing
tail -f ~/.openclaw/logs/worker-leak-guard.log
```

Every line names the pid, age and RSS, so the log doubles as a leak-rate
measurement: a healthy box after the source fix should trend toward
`killed=0`.
