#!/usr/bin/env bash
# discover_instances.sh — shared OpenClaw instance discovery for dinomem base + neuron
# installers. Makes "install & done" work on single-agent, multi-agent, AND
# multi-gateway hosts with zero brain from the installer (a noob).
#
# WHY: the installers historically assumed ONE config truth ($HOME/.openclaw/openclaw.json)
# and probed the DEFAULT gateway. On multi-instance hosts each agent has its own
# systemd unit exposing OPENCLAW_STATE_DIR + OPENCLAW_CONFIG_PATH + OPENCLAW_GATEWAY_PORT,
# and a matching openclaw.json — so the installer patched the WRONG config, missed the
# right agent, and false-reported "gateway not running". This lib is the single source
# of truth: it reads the systemd units (facts, not guesses) and lets the caller pick.
#
# Source this, then call:
#   discover_openclaw_instances            -> prints TSV rows: agent_id<TAB>state_dir<TAB>config<TAB>port
#   select_openclaw_instance [--instance ID] -> resolves selection; on success exports
#                                               DINOMEM_SEL_AGENT_ID / _STATE_DIR / _CONFIG / _PORT
#                                               (or, for "all", DINOMEM_SEL_ALL=1 + the TSV in DINOMEM_SEL_ALL_ROWS)
#   openclaw_running_for <state_dir>       -> instance-aware running probe (honors OPENCLAW_STATE_DIR)
#
# Fallback-safe: no `systemctl --user` (non-systemd host) => discover prints nothing,
# select returns "default" mode => caller keeps its existing single-config behavior.
# Nothing breaks for existing single-agent installs.

# ── discovery ────────────────────────────────────────────────────────────────
# Prints one TSV row per discovered instance:  agent_id \t state_dir \t config_path \t port
# Silent (no rows, rc 0) when systemctl is unavailable or no units match.
discover_openclaw_instances() {
  command -v systemctl >/dev/null 2>&1 || return 0
  # list-units (running) OR list-unit-files (installed-but-stopped) — union, dedup.
  local units unit
  # Match BOTH the suffixed per-agent units (openclaw-gateway-<id>.service) AND the
  # suffixless DEFAULT/main gateway (openclaw-gateway.service). The default unit is
  # the single most common single-box gateway and was previously invisible to the
  # regex `^openclaw-gateway-.*` (required a dash) — so main-gateway agents could
  # not be targeted at all. Glob 'openclaw-gateway*.service' catches both; sort -u dedups.
  units="$(
    { systemctl --user list-units --type=service --all --no-legend --plain 'openclaw-gateway*.service' 2>/dev/null
      systemctl --user list-unit-files --no-legend --plain 'openclaw-gateway*.service' 2>/dev/null
    } | awk '{print $1}' | grep -E '^openclaw-gateway(-[^.]+)?\.service$' | sort -u
  )"
  [ -n "$units" ] || return 0

  while IFS= read -r unit; do
    [ -n "$unit" ] || continue
    # Skip non-gateway helper units (watchdogs etc) that the glob may catch.
    case "$unit" in
      openclaw-gateway-watchdog.service|openclaw-gateway-split-watchdog.service) continue ;;
    esac
    # agent-id = unit name between 'openclaw-gateway-' and '.service'.
    # Suffixless default unit (openclaw-gateway.service) -> id 'main' (stable, non-empty).
    local aid="${unit#openclaw-gateway-}"; aid="${aid%.service}"
    [ "$aid" = "openclaw-gateway.service" ] && aid="main"
    case "$unit" in openclaw-gateway.service) aid="main" ;; esac
    # Pull the Environment= assignments the unit was installed with. `systemctl --user
    # show -p Environment` prints: Environment=KEY=val KEY2=val2 ... (space-separated).
    local envline state_dir config port
    envline="$(systemctl --user show -p Environment "$unit" 2>/dev/null)"
    envline="${envline#Environment=}"
    state_dir=""; config=""; port=""
    # shellcheck disable=SC2086
    local kv
    for kv in $envline; do
      case "$kv" in
        OPENCLAW_STATE_DIR=*)   state_dir="${kv#OPENCLAW_STATE_DIR=}" ;;
        OPENCLAW_CONFIG_PATH=*) config="${kv#OPENCLAW_CONFIG_PATH=}" ;;
        OPENCLAW_GATEWAY_PORT=*) port="${kv#OPENCLAW_GATEWAY_PORT=}" ;;
      esac
    done
    # Derive config from state_dir when the unit didn't set it explicitly.
    [ -z "$config" ] && [ -n "$state_dir" ] && config="$state_dir/openclaw.json"
    # DEFAULT/main gateway fallback: the suffixless unit typically sets neither
    # OPENCLAW_STATE_DIR nor OPENCLAW_CONFIG_PATH (it runs on OpenClaw's default
    # paths). Without this it would be dropped by the emptiness guard below and
    # main-gateway agents would stay untargetable. Resolve to the default home.
    if [ -z "$config" ] && [ "$aid" = "main" ]; then
      state_dir="${state_dir:-$HOME/.openclaw}"
      config="$state_dir/openclaw.json"
    fi
    # Skip units we couldn't resolve a config for (nothing to patch).
    [ -n "$config" ] || continue
    printf '%s\t%s\t%s\t%s\n' "$aid" "$state_dir" "$config" "$port"
  done <<< "$units"
  return 0
}

# ── instance-aware running probe ─────────────────────────────────────────────
# Honors the target instance's state dir so we probe the RIGHT gateway, not the
# default one. Empty state_dir => probe default (back-compat).
openclaw_running_for() {
  command -v openclaw >/dev/null 2>&1 || return 1
  local state_dir="$1"
  # Probe with `openclaw health` (a LIGHT liveness ping), NOT `openclaw status`.
  # WHY: `openclaw status` is a HEAVY command (fetches channel health + recent
  # sessions + model status). On a multi-gateway box, probing a NON-local gateway
  # by state-dir alone makes it HANG to the timeout -> false-negative "not
  # running" -> the installer silently SKIPS all config patches -> a half-wired
  # install (the exact "installer blunders on to an incomplete install" failure).
  # `openclaw health` just asks "is this gateway alive?" (internal durationMs ~86ms).
  # Redirect stdin from /dev/null so a status/health that ever waits on a TTY
  # can't block. Fall back to `status` only if `health` is unavailable (older CLI).
  #
  # TIMEOUT: the bottleneck is NOT the gateway (health answers in <100ms) but the
  # NODE CLI COLD-START (~10-12s: node boot + plugin load) on a busy box. A bare
  # `timeout 10` clips the health call MID-STARTUP -> false-negative "not running"
  # -> config patches skipped -> half-wired install. Use a generous bound
  # (AUTOSNAP-style override) and pass --timeout to `health` so its own connect
  # deadline is separate from the CLI-boot allowance. Default 45s covers a slow
  # cold start with headroom; override via DINOMEM_PROBE_TIMEOUT_S.
  local _pt="${DINOMEM_PROBE_TIMEOUT_S:-45}"
  local probe
  for probe in health status; do
    local _args=""
    [ "$probe" = health ] && _args="--timeout $(( _pt * 1000 ))"
    # shellcheck disable=SC2086
    if [ -n "$state_dir" ]; then
      OPENCLAW_STATE_DIR="$state_dir" timeout "$_pt" openclaw "$probe" $_args </dev/null >/dev/null 2>&1 && return 0
    else
      timeout "$_pt" openclaw "$probe" $_args </dev/null >/dev/null 2>&1 && return 0
    fi
  done
  return 1
}

# ── selection ────────────────────────────────────────────────────────────────
# Resolves which instance(s) to install into. Behavior by discovered count:
#   0  -> mode "default"  (caller keeps existing single-config path; unchanged)
#   1  -> auto-select it  (zero questions)
#   >=2 -> if --instance ID given, pick it; elif TTY, prompt ONCE (numbers + 'A) all');
#          else (non-interactive, no flag) -> fail with the list (never guess).
#
# On single/explicit selection, exports:
#   DINOMEM_SEL_AGENT_ID DINOMEM_SEL_STATE_DIR DINOMEM_SEL_CONFIG DINOMEM_SEL_PORT
#   DINOMEM_SEL_MODE=one
# On "all": DINOMEM_SEL_MODE=all + DINOMEM_SEL_ALL_ROWS=<TSV of every instance>
# On 0 instances: DINOMEM_SEL_MODE=default (nothing else set).
# Returns: 0 ok, 2 = non-interactive ambiguity (caller should abort), 3 = bad --instance.
select_openclaw_instance() {
  local want_id=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --instance) want_id="$2"; shift 2 ;;
      *) shift ;;
    esac
  done

  local rows; rows="$(discover_openclaw_instances)"
  local n; n="$(printf '%s\n' "$rows" | grep -c . || true)"

  # 0 instances -> default mode, caller unchanged.
  if [ "${n:-0}" -eq 0 ]; then
    export DINOMEM_SEL_MODE="default"
    return 0
  fi

  # Explicit --instance always wins (scripted/non-interactive path).
  if [ -n "$want_id" ]; then
    local row
    row="$(printf '%s\n' "$rows" | awk -F'\t' -v id="$want_id" '$1==id{print;exit}')"
    if [ -z "$row" ]; then
      echo "  [fail] --instance '$want_id' not found among discovered OpenClaw instances:" >&2
      printf '%s\n' "$rows" | awk -F'\t' '{printf "         - %s (%s)\n",$1,$3}' >&2
      return 3
    fi
    _dinomem_export_row "$row"; export DINOMEM_SEL_MODE="one"; return 0
  fi

  # Exactly 1 -> auto-select, zero questions.
  if [ "$n" -eq 1 ]; then
    _dinomem_export_row "$rows"; export DINOMEM_SEL_MODE="one"; return 0
  fi

  # >=2 and no --instance: prompt once IF interactive; else abort with the list.
  if [ ! -t 0 ]; then
    echo "  [fail] Multiple OpenClaw instances found and no --instance given (non-interactive):" >&2
    printf '%s\n' "$rows" | awk -F'\t' '{printf "         - %s (%s)\n",$1,$3}' >&2
    echo "         Re-run with: --instance <agent-id>   (or run interactively to choose)" >&2
    return 2
  fi

  # Interactive: one prompt, numbered + an 'A) all of them' choice baked in.
  echo "" >&2
  echo "Found $n OpenClaw instances:" >&2
  local i=1
  while IFS=$'\t' read -r aid sdir cfg port; do
    [ -n "$aid" ] || continue
    printf "  %d) %-20s %s\n" "$i" "$aid" "${sdir:-$cfg}" >&2
    i=$((i+1))
  done <<< "$rows"
  echo "  A) all of them" >&2
  local ans
  while :; do
    printf "Which to install into? [1-%d / A]: " "$n" >&2
    read -r ans || { ans=""; break; }
    case "$ans" in
      [Aa]) export DINOMEM_SEL_MODE="all" DINOMEM_SEL_ALL_ROWS="$rows"; return 0 ;;
      ''|*[!0-9]*) echo "  Enter a number 1-$n or A." >&2; continue ;;
      *) if [ "$ans" -ge 1 ] && [ "$ans" -le "$n" ]; then
           local row; row="$(printf '%s\n' "$rows" | sed -n "${ans}p")"
           _dinomem_export_row "$row"; export DINOMEM_SEL_MODE="one"; return 0
         else echo "  Out of range. 1-$n or A." >&2; fi ;;
    esac
  done
  # EOF on read with no valid answer -> treat as ambiguity abort (safer than guessing).
  echo "  [fail] No selection made." >&2
  return 2
}

# internal: split a TSV row into the DINOMEM_SEL_* exports.
_dinomem_export_row() {
  local row="$1"
  export DINOMEM_SEL_AGENT_ID; export DINOMEM_SEL_STATE_DIR
  export DINOMEM_SEL_CONFIG;   export DINOMEM_SEL_PORT
  DINOMEM_SEL_AGENT_ID="$(printf '%s' "$row" | cut -f1)"
  DINOMEM_SEL_STATE_DIR="$(printf '%s' "$row" | cut -f2)"
  DINOMEM_SEL_CONFIG="$(printf '%s' "$row" | cut -f3)"
  DINOMEM_SEL_PORT="$(printf '%s' "$row" | cut -f4)"
}

# ── Topology-aware auto-restart helpers (shared by base + neuron installers) ──
# WHY here: dinomem's config/plugin/hook wiring only applies after a gateway
# restart. Auto-restart makes it "install and done" — BUT on a multi-agent
# shared gateway one restart drops every agent on it (possibly different owners)
# at once, and an installer only owns one of them. So restart is TOPOLOGY-AWARE.
# These live in the shared lib (sourced by both install.sh scripts) so the logic
# exists once, not copy-pasted per repo. They read installer-scoped vars
# (AUTO_RESTART, OPENCLAW_JSON, DRY_RUN, OPENCLAW_STATE_DIR) from the caller.

# _agent_count <config_json>: how many agents this gateway config declares.
# 1 => safe to restart unilaterally (single tenant); >1 => shared gateway.
# On any parse failure prints 99 (fail-SAFE toward "multi" = do NOT auto-restart).
_agent_count() {
  local cfg="$1"
  [ -f "$cfg" ] || { echo 99; return; }
  python3 - "$cfg" <<'PYEOF' 2>/dev/null || echo 99
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    a = d.get('agents')
    if isinstance(a, dict): a = a.get('list', [])
    n = len(a) if isinstance(a, list) else 0
    print(n if n > 0 else 1)   # no agents array => single default agent
except Exception:
    print(99)
PYEOF
}

# _maybe_restart: the restart gate, called ONCE at the end of a successful install.
#   AUTO_RESTART=never  -> print the command, do nothing.
#   AUTO_RESTART=always -> restart IFF config validates.
#   AUTO_RESTART=auto   -> restart IFF single-agent gateway AND config validates.
# Validate is the load-bearing safety: a config failing schema would not come back
# after restart (total silence), so we NEVER restart past a failed validate in any
# mode. Honors OPENCLAW_STATE_DIR (right gateway on a multi-instance box). No-op
# under --dry-run. Defaults AUTO_RESTART=auto if the caller left it unset.
_maybe_restart() {
  [ "${DRY_RUN:-0}" = 1 ] && return 0
  command -v openclaw >/dev/null 2>&1 || return 0
  local mode="${AUTO_RESTART:-auto}"
  local restart_cmd="openclaw gateway restart"
  [ -n "${OPENCLAW_STATE_DIR:-}" ] && restart_cmd="OPENCLAW_STATE_DIR=$OPENCLAW_STATE_DIR openclaw gateway restart"

  if [ "$mode" = never ]; then
    echo "  → Restart to apply dinomem changes:  $restart_cmd"
    return 0
  fi

  local n; n="$(_agent_count "${OPENCLAW_JSON:-$HOME/.openclaw/openclaw.json}")"
  if [ "$mode" = auto ] && [ "$n" != 1 ]; then
    echo "  → Multi-agent gateway ($n agents) — NOT auto-restarting (would drop the others)."
    echo "    Restart when ready:  $restart_cmd    (or re-run with --restart to force)"
    return 0
  fi

  # always, or auto+single-agent: validate FIRST, then restart.
  if ! openclaw config validate >/dev/null 2>&1; then
    echo "  ✖ Config FAILS validation — NOT restarting (gateway would not come back)."
    echo "    Fix, then:  openclaw config validate && $restart_cmd"
    return 0
  fi
  echo "  ↻ Config valid — restarting gateway to apply dinomem changes…"
  if eval "timeout 90 $restart_cmd" >/dev/null 2>&1; then
    echo "  ✓ Gateway restarted — dinomem is live. Install and done."
  else
    echo "  ! Restart command returned nonzero — verify manually:  $restart_cmd"
  fi
}
