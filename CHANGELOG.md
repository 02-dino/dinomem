# Changelog

## 1.1.2

### Changed
- README: added an explicit warning in the "Memory pinning" section that `memory/`
  is cron-managed. Only `_`-prefixed files (e.g. `_pin_*.md`) are protected from
  cleanup; hand-dropped untagged `.md` files may be deduped, TTL-expired, bootcheck-
  removed, or daily-flush pruned. Documents the per-pattern retention rules
  (`_pin_`, `_note_`, bare `YYYY-MM-DD.md`, extraction files, `MEMORY.md`).

## 1.1.1

### Fixed
- **Gateway crash on install (invalid config key).** `install.sh` wrote
  `agents.defaults.workspaceBootstrap = "always"`, but `workspaceBootstrap` is not
  a valid OpenClaw config key (it does not exist in the schema on any version).
  Because `agents.defaults` is `additionalProperties: false`, the unknown key made
  the gateway reject `openclaw.json` and crash on load (reported on OpenClaw 2026.6.1).
  - The valid key is `contextInjection`. `install.sh` now sets
    `agents.defaults.contextInjection = "always"` (already the OpenClaw default;
    set explicitly to document intent) and strips any legacy `workspaceBootstrap`
    key left by older installs so the config validates.
  - `uninstall.sh` now reverts `contextInjection` and also removes the legacy
    `workspaceBootstrap` key if present.
  - Affected users can self-fix without reinstalling: delete `workspaceBootstrap`
    from `agents.defaults` in `~/.openclaw/openclaw.json` and restart the gateway.

### Changed
- README: Prerequisites now state the minimum OpenClaw version (>= 2026.1.0) for the
  `memorySearch` / `compaction` / `contextInjection` config keys. Config-patch table
  row updated from `workspaceBootstrap` to `contextInjection` with the crash rationale.

## 1.1.0

### Added
- **startupContext + guarded daily flush** (enabled by default). On `/new` and
  `/reset`, the last 2 days of raw daily memory are injected on top of the
  always-injected `MEMORY.md` index. `memory_search` pull still handles deep recall.
  - `install.sh` enables OpenClaw `memoryFlush` with a guard prompt that confines
    it to writing the bare daily file `memory/YYYY-MM-DD.md` and forbids editing
    `MEMORY.md` (which dinomem auto-generates and would overwrite).
  - `install.sh` enables `startupContext` (`dailyMemoryDays: 2`).
  - New `procedures/cleanup_startup_daily.py` + cron (02:05 UTC) prunes bare
    `YYYY-MM-DD.md` files older than 2 days. It never touches per-item files,
    `_pin_*`, `_note_*`, or `MEMORY.md`.

### Fixed
- `memory_cleanup.py` now skips bare `YYYY-MM-DD.md` files in the dedup, TTL, and
  bootcheck passes. Previously the bootcheck pass could delete an untagged flush
  file that happened to mention a framework keyword.

### Changed
- README, `references/architecture.md`, and config-patch tables updated: the
  `memoryFlush`/`startupContext` rows now reflect the enabled-by-default behavior
  and document the underscore-vs-hyphen filename matcher and the guard rationale.

### Upgrading
Existing installs: run `bash scripts/update.sh` (pulls latest, re-runs the
installer with `--force`). This re-patches `openclaw.json` (memoryFlush prompt +
startupContext), installs `cleanup_startup_daily.py`, and registers its cron.
Memory data and logs are preserved.

## 1.0.0

- Initial release.
