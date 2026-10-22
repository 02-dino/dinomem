#!/bin/sh
# gate/improvement-digest.sh [workspace]
# T1 weekly backstop for the auto self-improvement lane.
#
# CONTRACT: prints ONLY when something is actually waiting. Empty stdout = no
# work = zero model spend, same as every other gate/ template.
#
# WHY A WEEKLY DIGEST EXISTS AT ALL: the daily lane announces a proposal once,
# when it is created. If that one message is missed, the proposal waits forever
# and the queue silently grows -- which is how a review gate stops being read.
# This is the catch-up: it re-surfaces whatever is still unanswered.
#
# It also runs the skill curator first, because an archive proposal is reviewed
# through the same gate and should ride the same weekly message rather than
# interrupt a working day on its own.
set -eu

ws="${1:-${DINOMEM_WORKSPACE:-}}"
[ -n "$ws" ] || { echo "usage: improvement-digest.sh <workspace>" >&2; exit 2; }
[ -d "$ws" ] || { echo "workspace not found: $ws" >&2; exit 2; }

py="${DINOMEM_PY:-python3}"
export DINOMEM_WORKSPACE="$ws"

# 1. Skill curator. Zero LLM. Fail-closed internally (no usage data or an
#    unreadable cron list -> proposes nothing), so it is safe to run blind.
"$py" "$ws/procedures/skill_curator.py" propose >/dev/null 2>&1 || true

# 2. Anything still awaiting a decision? rc!=0 means the queue is empty.
pending=$("$py" "$ws/tools/route.py" pending 2>/dev/null) || exit 0
[ -n "$pending" ] || exit 0

# Deliberately language-NEUTRAL scaffolding. Owner-facing PROSE is written by
# the model (the skill tells it to match the owner's own language, read from the
# session); this file is shared by every install, so hardcoding one human
# language here would be the same mistake as hardcoding one chat channel.
# `route.py pending` already prints its own command hints, so this adds only
# what it cannot know: that nothing has happened yet and a no is a valid answer.
cat <<EOF
[auto-improve] Proposals are waiting for your decision.

$pending

Nothing changes until you approve. Rejecting is useful signal, not a failure.
EOF
