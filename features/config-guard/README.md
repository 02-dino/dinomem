# config-guard — crash-loop protection for `openclaw.json`

An **independent systemd watchdog** that reverts `openclaw.json` to the last
known-good snapshot the instant a write leaves it **syntactically broken** — so a
stray trailing comma can never crash-loop your gateway.

## Why this exists

`openclaw.json` is the gateway's rulebook. A syntax-broken write (most common: a
trailing comma before `}` or `]`) makes the gateway **fail to load config** on the
next reload/restart. With `Restart=always`, that becomes a **crash-loop** and every
channel dies until the file is hand-fixed.

This is not theory — it's a real incident (2026-07-15): an agent edit left a
trailing comma, the gateway restart-looped until the file was repaired.

The guard runs at the **OS/systemd level, outside OpenClaw**, so it keeps working
even while OpenClaw itself is crashing, hanging, or restarting.

## Two layers of defense (use both)

| | `openclaw config patch` (in the agent) | config-guard (systemd) |
|---|---|---|
| **Nature** | **Prevention** at the source | **Enforcement / safety net** |
| **Depends on** | the writer obeying | nothing / no one |
| **Agent forgets / uses another tool** | ❌ bypassed | ✅ still caught |
| **You edit by hand (vim/sed)** | ❌ unprotected | ✅ still caught |
| **A plugin/cron writes config** | ❌ unaware of the rule | ✅ still caught |
| **When it acts** | before the write (clean) | after the write (~2–4s, then revert) |

`config patch` = clean prevention **when obeyed**; the guard = a net that runs **no
matter how** the config got broken. dinomem already ships the prevention side
(`gate_lib.sh` routes config writes through `config patch`); this feature adds the
enforcement side.

## Safe-by-design

- **Auto-restore ONLY on JSON *syntax* breakage** — a real, unambiguous crash risk.
- **Syntax OK but schema-invalid** (`openclaw config validate` fails) → **WARN-log
  only, never restore** — so a legitimate valid edit is never silently undone.
- **Needs ≥1 good snapshot** (`.guard-good`); it refreshes automatically every time
  the config is seen valid.

## Install

```bash
bash install.sh                 # user systemd unit (default, no root)
bash install.sh --system        # system unit (gateway runs as a system service)
bash install.sh --openclaw-dir /path/to/.openclaw   # non-default location
```

The installer is **dup-aware**: if a guard script or units already exist, it leaves
them alone unless you pass `--force`. Re-running is a safe no-op. It also runs a
**mandatory self-test on a fake file before arming** — if the guard wouldn't have
restored a broken config, it refuses to enable the watcher.

Requires `jq` (auto-installed if missing). `flock` recommended. Degrades
gracefully if `systemd` is unavailable (installs the script; you wire your own
watch).

## Verify / operate

```bash
systemctl --user is-active openclaw-config-guard.path   # -> active (waiting)
tail -f ~/.openclaw/logs/config-guard.log               # see every rescue
ls ~/.openclaw/openclaw.json.broken-*                    # forensic copies of bad writes
```

## Test it yourself (zero risk to the real config)

```bash
T=$(mktemp -d)
echo '{"ok":true}' > "$T/fake.json"
cp "$T/fake.json" "$T/fake.json.guard-good"
printf '%s' '{"ok":true,,}' > "$T/fake.json"     # corrupt it
GUARD_CFG="$T/fake.json" GUARD_GOOD="$T/fake.json.guard-good" \
  GUARD_LOG="$T/log" GUARD_LOCK="$T/lock" GUARD_SETTLE=0 \
  ~/.openclaw/scripts/config-guard.sh
cat "$T/fake.json"        # -> {"ok":true}  (restored)
```

## Limitations (honest)

- **Reactive:** a broken file exists on disk ~2–4s before revert. Still safe — the
  real danger (crash) only happens on gateway **restart**, a much longer window, so
  the guard almost always wins the race.
- **temp-rename blind spot:** a tool that writes via "temp file then rename" can slip
  past the inotify watch on the file. Complement it by writing config through
  `openclaw config patch` (validated writes).
- **Syntax errors only.** Schema errors (valid syntax, wrong structure) are logged,
  not reverted — so valid edits are never destroyed.

## Uninstall (fully reversible)

```bash
bash install.sh --uninstall            # (--system if you installed with --system)
```

Removes the units + script + good-snapshot. Keeps any `.broken-*` forensic copies.
