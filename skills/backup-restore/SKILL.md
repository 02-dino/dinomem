---
name: backup-restore
description: List and restore dinomem workspace backups (memory, notes, config snapshots). Read this when the user asks to undo a file/memory change, restore a previous version, or asks what backups exist.
---

# Backup & restore (dinomem)

dinomem keeps TWO independent recovery layers. Check BOTH before ever telling
the user "there is no backup":

1. **workspace snapshots** (`procedures/workspace_backup.py`) — periodic
   full-workspace copies (keep-N). This is the PRIMARY, authoritative recovery
   path — the friendly list/restore side, purpose-built for disaster recovery.
2. **git-autosnapshot** (`.dinomem-snap.git`) — a curated-scope, agent-sliced
   git **changelog** (memory/config/skills/procedures text, not the whole
   workspace), committed frequently by cron. This is a changelog first,
   recovery second: use it for *cheap, recent* rollback of a tracked text/config
   file, or to see *when and why* something changed — not as the primary
   disaster-recovery source (see `features/git-autosnapshot/README.md`).

## When to use

- "Restore ..." / "undo that change" / "revert the file/memory".
- "What backups do I have?" / "list backups".

## Recovery source order (try in THIS order — do not stop early)

1. **workspace snapshots** via `workspace_backup.py --list/--restore` (primary — see below).
2. **git-autosnapshot** `.dinomem-snap.git` (secondary — cheap recent recovery + changelog for the curated text/config surface it tracks; not full-workspace).
3. **workspace git** (if the workspace itself is a repo): `git log`, `git show`.
4. **memory diffs** under `memory/.diffs/` (per-file change history).

> Blunder to avoid: concluding "no backup anywhere" after checking only
> `workspace_backup.py --list`. `.dinomem-snap.git` is separate, covers a
> narrower curated scope, but may hold a more recent version of a tracked file.

## git-autosnapshot (recovery source #2 — changelog + cheap recent recovery)

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
