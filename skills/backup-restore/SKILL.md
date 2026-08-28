---
name: backup-restore
description: List and restore dinomem workspace backups (memory, notes, config snapshots). Read this when the user asks to undo a file/memory change, restore a previous version, or asks what backups exist.
---

# Backup & restore (dinomem)

dinomem keeps TWO independent backup layers. Check BOTH before ever telling the
user "there is no backup":

1. **git-autosnapshot** (`.dinomem-snap.git`) — a byte-exact, timestamped git
   mirror of the whole workspace (thousands of files), committed frequently by
   cron. Look here FIRST: most complete + most exact, and it holds files the
   periodic snapshot may not.
2. **workspace snapshots** (`procedures/workspace_backup.py`) — periodic
   full-workspace copies (keep-N), the friendly list/restore side.

## When to use

- "Restore ..." / "undo that change" / "revert the file/memory".
- "What backups do I have?" / "list backups".

## Recovery source order (try in THIS order — do not stop early)

1. **git-autosnapshot** `.dinomem-snap.git` (byte-exact, most complete) — see below.
2. **workspace snapshots** via `workspace_backup.py --list/--restore`.
3. **workspace git** (if the workspace itself is a repo): `git log`, `git show`.
4. **memory diffs** under `memory/.diffs/` (per-file change history).

> Blunder to avoid: concluding "no backup anywhere" after checking only the
> `backups/` folder. `.dinomem-snap.git` is separate and usually has the file.

## git-autosnapshot (recovery source #1)

List snapshots/commits:
```bash
git --git-dir=DINOMEM_WORKSPACE_PLACEHOLDER/.dinomem-snap.git log --all --oneline | head -40
```

Find a file's path at a commit:
```bash
git --git-dir=DINOMEM_WORKSPACE_PLACEHOLDER/.dinomem-snap.git ls-tree -r --name-only <sha> | grep <name>
```

Restore ONE file (byte-exact) from commit `<sha>`:
```bash
git --git-dir=DINOMEM_WORKSPACE_PLACEHOLDER/.dinomem-snap.git show <sha>:<relative/path> > DINOMEM_WORKSPACE_PLACEHOLDER/<relative/path>
```
Verify with `diff` before trusting. If `.dinomem-snap.git` is absent, fall
through to the workspace-snapshot side.

## Commands (workspace snapshots)

Run from the workspace root.

**List available snapshots:**
```bash
python3 DINOMEM_WORKSPACE_PLACEHOLDER/procedures/workspace_backup.py --list
```

**Restore an entire snapshot** (by index or name from `--list`; defaults to latest):
```bash
python3 DINOMEM_WORKSPACE_PLACEHOLDER/procedures/workspace_backup.py --restore [index|name] [--yes]
```

**Restore a single file** from a snapshot:
```bash
python3 DINOMEM_WORKSPACE_PLACEHOLDER/procedures/workspace_backup.py --restore [index|name] --file <relative/path>
```
e.g. `--file memory/2026-06-01.md`

## Notes

- Backups auto-run via cron; this tool is the read/restore side.
- A full restore overwrites current files — confirm the target with `--list`
  first, and prefer `--file` when only one file needs rolling back.
