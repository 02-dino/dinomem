---
name: backup-restore
description: List and restore dinomem workspace backups (memory, notes, config snapshots). Read this when the user asks to undo a file/memory change, restore a previous version, or asks what backups exist.
---

# Backup & restore (dinomem)

dinomem snapshots the workspace on a cron. Use `procedures/workspace_backup.py`
to inspect and restore those snapshots.

## When to use

- "Restore ..." / "undo that change" / "revert the file/memory".
- "What backups do I have?" / "list backups".

## Commands

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
