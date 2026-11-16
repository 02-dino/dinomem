#!/usr/bin/env bash
# check_stalls.sh — zero-LLM pre-gate for the Stall Rescuer lane of cron_gate.sh.
# Exit 0 = at least one NEW genuine subagent stall (JSON on stdout, already
#          written to the ledger so it fires once); exit 1 = nothing to do.
# All logic lives in stall_watch.py (one engine, selftested); this wrapper only
# adds the gate-harness conventions shared with check_projects.sh:
#   - fail-open on a missing engine/python (exit 1 = lane quiet, never a crash)
#   - CPU backpressure via gate_lib's gate_fire_or_defer when available
#     (base ships scripts/lib/gate_lib.sh; sourced directly, no neuron shim).
# WHY no refire guard here: the ledger already guarantees each stall fires ONCE,
# and a deferred tick must re-offer the same stall -> --dry-run while busy.
WS="${OPENCLAW_WORKSPACE:-$HOME/.openclaw/workspace}"
export OPENCLAW_WORKSPACE="$WS"
SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENGINE="$SCRIPTS_DIR/stall_watch.py"
[ -f "$ENGINE" ] && command -v python3 >/dev/null 2>&1 || exit 1
# shellcheck source=lib/gate_lib.sh
[ -r "$SCRIPTS_DIR/lib/gate_lib.sh" ] && source "$SCRIPTS_DIR/lib/gate_lib.sh" 2>/dev/null
python3 "$ENGINE" gate --dry-run >/dev/null 2>&1 || exit 1
if declare -F gate_fire_or_defer >/dev/null 2>&1; then
  gate_fire_or_defer "${DINOMEM_RESCUER_LOAD_CEILING:-2.0}" || exit 1
fi
exec python3 "$ENGINE" gate
