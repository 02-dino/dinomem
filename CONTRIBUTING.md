# Contributing to dinomem

## Before you PR

1. **No hardcoded paths** — use placeholders (see table below), never `workspace-analyst`, `agent:analyst`, `/home/linuxbrew/...`
2. **stdlib only** — scripts must not require pip installs unless absolutely necessary
3. **Test install.sh** on a clean workspace before submitting
4. **Update README** if you change any user-facing behavior

## Placeholders

All placeholders are replaced by `install.sh` at install time via `sed`:

| Placeholder | Replaced with |
|-------------|---------------|
| `DINOMEM_WORKSPACE_PLACEHOLDER` | `<workspace>` absolute path |
| `DINOMEM_AGENT_ID_PLACEHOLDER` | agent ID string |
| `DINOMEM_AGENT_SESSIONS_PLACEHOLDER` | sessions directory path |

### Workspace resolution fallback

Scripts that need the workspace path resolve it in this priority order:

1. `DINOMEM_WORKSPACE` environment variable, if set (runtime override)
2. The `sed`-substituted `DINOMEM_WORKSPACE_PLACEHOLDER` value (normal install)
3. Self-locate from the script's own location (`Path(__file__).resolve().parent.parent`)

Step 3 is a safety net: if the install-time `sed` is skipped or fails (manual
copy, partial install, moved workspace dir), the script still finds its workspace
instead of dying on a literal placeholder string. Normal installs are unaffected
— `sed` runs, step 2 wins, and the `grep PLACEHOLDER` test below still returns
nothing. Only `DINOMEM_WORKSPACE_PLACEHOLDER` has this self-heal fallback; the
agent-id and sessions placeholders cannot be self-located and still require `sed`.

## Testing locally

```bash
# Test install on a clean temp workspace
bash scripts/install.sh \
  --workspace /tmp/test-dinomem \
  --agent-id test \
  --no-docker \
  --no-cron \
  --force

# Verify placeholders were replaced
grep -r "PLACEHOLDER" /tmp/test-dinomem/
# Should return nothing
```

## Reporting bugs

Use the GitHub issue template. Include:
- OS and version
- OpenClaw version (`openclaw --version`)
- Relevant log output from `<workspace>/logs/`
