#!/usr/bin/env bash
# dinomem-undo — friendly recovery front-end for the git-autosnapshot store.
#
# The snapshot history lives in an ISOLATED git-dir (.dinomem-snap.git) addressed
# via --git-dir/--work-tree, so the raw recovery command is long and easy to fumble.
# This wrapper hides that: list snapshots, show what changed, and restore a path
# (or everything) to an earlier snapshot — without ever touching your own repo.
#
# Usage:
#   dinomem-undo [--repo DIR] list [N]              # last N snapshots (default 15)
#   dinomem-undo [--repo DIR] show <ref>            # files changed in that snapshot
#   dinomem-undo [--repo DIR] diff <ref> [path]     # diff a snapshot vs now
#   dinomem-undo [--repo DIR] restore <ref> [path]  # restore path (default: memory/) to <ref>
#   dinomem-undo [--repo DIR] restore-all <ref>     # restore the WHOLE tree to <ref>
#
#   <ref> is any snapshot id from `list` (e.g. a short sha), or HEAD~2, etc.
#   --repo defaults to $OPENCLAW_HOME or ~/.openclaw.
#
# SAFETY: restore only ever touches the path(s) you name (default memory/). It
# NEVER force-resets history and NEVER writes to your own $REPO/.git.
set -euo pipefail

REPO="${OPENCLAW_HOME:-$HOME/.openclaw}"
GIT_DIR=""

# Allow --repo / --git-dir before the subcommand.
while [ $# -gt 0 ]; do
  case "${1:-}" in
    --repo)    REPO="$2"; shift 2 ;;
    --git-dir) GIT_DIR="$2"; shift 2 ;;
    *) break ;;
  esac
done

REPO="$(cd "$REPO" 2>/dev/null && pwd || echo "$REPO")"
[ -z "$GIT_DIR" ] && GIT_DIR="$REPO/.dinomem-snap.git"

if [ ! -f "$GIT_DIR/HEAD" ]; then
  echo "dinomem-undo: no snapshot store at $GIT_DIR" >&2
  echo "  (is git-autosnapshot installed for this repo?)" >&2
  exit 2
fi

# All git calls addressed at the isolated store + repo work-tree.
g() { git --git-dir="$GIT_DIR" --work-tree="$REPO" "$@"; }

CMD="${1:-list}"; shift || true

case "$CMD" in
  list|ls)
    N="${1:-15}"
    echo "Last $N snapshots in $GIT_DIR:"
    g log -n "$N" --format='  %C(yellow)%h%C(reset)  %ad  %s' --date=format:'%Y-%m-%d %H:%M' 2>/dev/null \
      || g log -n "$N" --format='  %h  %ad  %s' --date=short
    ;;
  show)
    REF="${1:?usage: dinomem-undo show <ref>}"
    echo "Files changed in $REF:"
    g show --stat --format='  %h  %ad  %s%n' --date=format:'%Y-%m-%d %H:%M' "$REF"
    ;;
  diff)
    REF="${1:?usage: dinomem-undo diff <ref> [path]}"; shift || true
    if [ $# -gt 0 ]; then g diff "$REF" -- "$@"; else g diff "$REF"; fi
    ;;
  restore)
    REF="${1:?usage: dinomem-undo restore <ref> [path]}"; shift || true
    if [ $# -eq 0 ]; then set -- "memory/"; fi
    echo "Restoring the following to $REF (your own repo untouched):"
    printf '  %s\n' "$@"
    g checkout "$REF" -- "$@"
    echo "Done. Review with: git --git-dir=$GIT_DIR --work-tree=$REPO status"
    ;;
  restore-all)
    REF="${1:?usage: dinomem-undo restore-all <ref>}"
    echo "Restoring the ENTIRE work-tree to $REF."
    echo "This overwrites current files with their $REF versions (files not in $REF are left as-is)."
    g checkout "$REF" -- .
    echo "Done. Review with: git --git-dir=$GIT_DIR --work-tree=$REPO status"
    ;;
  -h|--help|help)
    grep -E '^#( |$)' "$0" | sed 's/^# \{0,1\}//'
    ;;
  *)
    echo "dinomem-undo: unknown command '$CMD' (try: list | show | diff | restore | restore-all)" >&2
    exit 2
    ;;
esac
