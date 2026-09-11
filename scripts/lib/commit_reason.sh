#!/usr/bin/env bash
# commit_reason.sh — thin SHELL wrapper around procedures/commit_reason.py drop().
#
# WHY THIS EXISTS
#   The Python callers (tools/*_tool.py, procedures/extract_memory.py,
#   procedures/memory_review.py) import commit_reason.drop() directly — they do
#   NOT need this file. This wrapper is for a SHELL caller (a hook handler, an
#   install step, a future .sh mutation) that wants to drop the same semantic
#   commit-subject hint without re-implementing the path contract. It just execs
#   the one real writer, so there is exactly ONE source of truth for the hint
#   format + file location (DRY): the .py.
#
# USAGE
#   commit_reason.sh "<verb>: <subject> [<detail>]"
#     verb ∈ {memory|config|skill|hook|cron|...} — same convention the Python
#     callers use. Multiple invocations in one 15-min tick APPEND (line 1 =
#     commit subject, lines 2+ = body); see procedures/commit_reason.py.
#
# CONTRACT
#   Fail-open ALWAYS. A commit-subject hint is cosmetic; it must never block,
#   slow, or crash the mutation it decorates. Any failure (no python3, missing
#   writer, bad args) is swallowed and we exit 0 so a caller that does
#   `commit_reason.sh "..." || true` — or even forgets the guard — is never hurt.
set -u

# Locate the real writer relative to THIS file: scripts/lib/ -> ../../procedures/.
_here="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" >/dev/null 2>&1 && pwd)"
_writer="$_here/../../procedures/commit_reason.py"

# Nothing to drop / no python / no writer -> silently succeed (fail-open).
[ "$#" -ge 1 ] && [ -n "${1:-}" ] || exit 0
command -v python3 >/dev/null 2>&1 || exit 0
[ -f "$_writer" ] || exit 0

# The .py CLI already clean_subject()s, bounds, de-dupes and appends; pass all
# args through as ONE reason. Swallow its exit + any stderr — cosmetic hint only.
python3 "$_writer" "$@" >/dev/null 2>&1 || true
exit 0
