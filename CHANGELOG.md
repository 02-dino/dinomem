# Changelog

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
