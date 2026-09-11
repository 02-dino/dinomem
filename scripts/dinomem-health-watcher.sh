#!/bin/bash
# dinomem-health-watcher.sh — per-agent dinomem health report, portable across
# single-agent / multi-agent, Linux / macOS, base-only / neuron installs.
#
# WHAT IT CHECKS (per discovered dinomem agent):
#   - Gateway: process/service up + (if known) listen port responding.
#   - Memory DB: SQLite opens read-only (SELECT 1).
#   - Core cron freshness: auto_session_reset / memory_cleanup / memory_review
#     (the three crons install.sh wires on EVERY base install).
# Plus a VPS resource line, a recall-activity line (from kb/retrieval_log/, base),
# and — only when the neuron upgrade is present — a memory-integrity line.
#
# DELIVERY (owner choice B->A): if an openclaw.json with a usable Telegram bot
# token is found (and a chat id is configured), send the report to Telegram;
# otherwise AUTO-FALL BACK to stdout + log file (cron can mail it). No config =
# no crash, just prints.
#
# NEURON-GRACEFUL: base installs lack some tools the fuller report uses. Every
# neuron-only probe is feature-detected and SKIPPED cleanly when absent — the
# script never errors because a neuron file is missing.
#
# SCHEDULE: NOT wired by default. install.sh --health-cron opts in (daily 07:00
# local). The smart-notify + cooldown logic also makes it safe at higher
# frequency (e.g. every 30 min) if you tighten the interval yourself.
#
# Templated at install time: DINOMEM_WORKSPACE_PLACEHOLDER (primary agent
# workspace) + DINOMEM_AGENT_ID_PLACEHOLDER. These are DEFAULTS/hints only —
# the script still auto-discovers every dinomem agent at runtime, so a
# multi-agent box is covered even though install baked one primary.
#
# Usage:
#   bash scripts/dinomem-health-watcher.sh            # smart notify (default)
#   bash scripts/dinomem-health-watcher.sh --dry-run  # print, never send
#   bash scripts/dinomem-health-watcher.sh --force    # send regardless of state
#   bash scripts/dinomem-health-watcher.sh --always   # notify every run (legacy)
#   bash scripts/dinomem-health-watcher.sh --stdout   # force stdout, skip Telegram
#
# Env overrides (all optional):
#   OPENCLAW_HOME                 default ~/.openclaw
#   DINOMEM_WATCHER_SKIP         space/comma agent ids to exclude
#   DINOMEM_WATCHER_TELEGRAM_ACCOUNT / _CHAT_ID / _TOPIC_ID
#   DINOMEM_WATCHER_CONFIG       openclaw.json path (default $OPENCLAW_HOME/openclaw.json)
#   DINOMEM_ALERT_COOLDOWN_SEC   reminder cooldown for unchanged problems (default 21600)
#   DINOMEM_WATCHER_GRACE_H      suppress never-ran flags on fresh installs (default 48)

set -uo pipefail

# ── Path + config resolution (portable, no hardcoded box paths) ───────────────
# OPENCLAW_HOME ladder: env > the templated workspace's parent > ~/.openclaw.
_TEMPLATED_WS="DINOMEM_WORKSPACE_PLACEHOLDER"
if [ -n "${OPENCLAW_HOME:-}" ]; then
  OC="$OPENCLAW_HOME"
elif [ -d "$_TEMPLATED_WS" ]; then
  OC="$(cd "$_TEMPLATED_WS/.." 2>/dev/null && pwd)"
else
  OC="$HOME/.openclaw"
fi
PRIMARY_AGENT="DINOMEM_AGENT_ID_PLACEHOLDER"
# If install.sh never sed-replaced the placeholders (e.g. a hand-copied script),
# fall back to sane runtime defaults instead of emitting the literal token.
case "$PRIMARY_AGENT" in *_PLACEHOLDER) PRIMARY_AGENT="main" ;; esac
case "$_TEMPLATED_WS" in *_PLACEHOLDER) _TEMPLATED_WS="" ;; esac

LOG_FILE="${DINOMEM_HEALTH_LOG:-$OC/logs/dinomem-health-watcher.log}"
STATE_FILE="${DINOMEM_HEALTH_STATE:-$OC/logs/dinomem-health-watcher.state}"
ALERT_COOLDOWN_SEC="${DINOMEM_ALERT_COOLDOWN_SEC:-21600}"
TELEGRAM_CONFIG="${DINOMEM_WATCHER_CONFIG:-$OC/openclaw.json}"
TELEGRAM_ACCOUNT="${DINOMEM_WATCHER_TELEGRAM_ACCOUNT:-}"
TELEGRAM_CHAT_ID="${DINOMEM_WATCHER_TELEGRAM_CHAT_ID:-}"
TELEGRAM_TOPIC_ID="${DINOMEM_WATCHER_TELEGRAM_TOPIC_ID:-}"

# systemd --user from cron often lacks XDG_RUNTIME_DIR (false "gateway down").
if [ -z "${XDG_RUNTIME_DIR:-}" ] && [ "$(id -u)" -eq 0 ]; then
  export XDG_RUNTIME_DIR="/run/user/0"
fi
if [ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ] && [ -n "${XDG_RUNTIME_DIR:-}" ]; then
  export DBUS_SESSION_BUS_ADDRESS="unix:path=${XDG_RUNTIME_DIR}/bus"
fi

DRY_RUN=false
FORCE=false
FORCE_STDOUT=false
NOTIFY_MODE="smart"
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=true ;;
    --force) FORCE=true ;;
    --stdout) FORCE_STDOUT=true ;;
    --smart|--only-on-change) NOTIFY_MODE="smart" ;;
    --always) NOTIFY_MODE="always" ;;
    -h|--help) sed -n '2,45p' "$0"; exit 0 ;;
  esac
done

IS_MACOS=false
[ "$(uname 2>/dev/null)" = "Darwin" ] && IS_MACOS=true

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') [dinomem-health] $*"; }

# ── Agent discovery (runtime, portable — the single/multi-agent generalization) ─
# An agent is a dinomem install when its workspace AGENTS.md carries the base
# managed marker "BEGIN:dinomem ". We scan every candidate workspace dir under
# OPENCLAW_HOME so ONE codepath covers a single-agent box (one match) and a
# multi-agent box (many). No hardcoded agent list, no per-agent port/db/service
# overrides — everything below is resolved from the filesystem at runtime.
#
# Candidate workspaces (in priority order, dedup'd):
#   - $OC/workspace-<id>   (multi-agent convention)
#   - $OC/workspace        (plain single-agent default from install.sh)
#   - the templated primary workspace (baked by install; may live elsewhere)
declare -a AGENTS=()
declare -A AGENT_WS=()
declare -A AGENT_DB=()

declare -A _SKIP=()
for _s in ${DINOMEM_WATCHER_SKIP:+${DINOMEM_WATCHER_SKIP//,/ }}; do
  [ -n "$_s" ] && _SKIP["$_s"]=1
done

_has_dinomem_marker() { grep -q 'BEGIN:dinomem ' "$1/AGENTS.md" 2>/dev/null; }

# Derive an agent id from a workspace path: workspace-<id> -> <id>; a plain
# 'workspace' dir -> the templated primary id if set, else 'main'.
_agent_id_for_ws() {
  local ws="$1" base
  base="$(basename "$ws")"
  case "$base" in
    workspace-*) echo "${base#workspace-}" ;;
    workspace)   echo "${PRIMARY_AGENT:-main}" ;;
    *)           echo "$base" ;;
  esac
}

# Resolve a memory DB path for an agent, trying the two conventions install.sh
# uses, then a symlink-followed check. Empty if none found (DB check then skips).
_resolve_db() {
  local aid="$1" cand
  for cand in \
    "$OC/agents/$aid/agent/openclaw-agent.sqlite" \
    "$OC/memory/$aid.sqlite"; do
    if [ -e "$cand" ] || [ -L "$cand" ]; then echo "$cand"; return 0; fi
  done
  echo ""
}

_register_agent() {
  local ws="$1" aid
  [ -d "$ws" ] || return 0
  _has_dinomem_marker "$ws" || return 0
  aid="$(_agent_id_for_ws "$ws")"
  [ -n "${_SKIP[$aid]:-}" ] && return 0
  [ -n "${AGENT_WS[$aid]:-}" ] && return 0   # already registered (dedup)
  AGENTS+=("$aid")
  AGENT_WS[$aid]="$ws"
  AGENT_DB[$aid]="$(_resolve_db "$aid")"
}

for _ws in "$OC"/workspace-* "$OC/workspace" "$_TEMPLATED_WS"; do
  _register_agent "$_ws"
done

# Stable ordering for deterministic report + fingerprint.
if [ ${#AGENTS[@]} -gt 0 ]; then
  IFS=$'\n' AGENTS=($(printf '%s\n' "${AGENTS[@]}" | LC_ALL=C sort)); unset IFS
fi

declare -A OVERALL=()
declare -A REPORT_BLOCKS=()
PROBLEM_ISSUES=()

# ── Severity helpers ───────────────────────────────────────────────
severity_rank() { case "$1" in OK) echo 0 ;; WARN) echo 1 ;; ERROR) echo 2 ;; *) echo 1 ;; esac; }
max_severity() {
  if [ "$(severity_rank "$1")" -ge "$(severity_rank "$2")" ]; then echo "$1"; else echo "$2"; fi
}
agent_status_emoji() {
  case "$1" in OK) echo "✅" ;; WARN) echo "⚠️" ;; ERROR) echo "🔴" ;; *) echo "⚠️" ;; esac
}

record_problem_issue() {
  local existing
  for existing in "${PROBLEM_ISSUES[@]}"; do [ "$existing" = "$1" ] && return 0; done
  PROBLEM_ISSUES+=("$1")
}

has_problems() {
  local agent
  for agent in "${AGENTS[@]}"; do
    [ "${OVERALL[$agent]:-OK}" != "OK" ] && return 0
  done
  return 1
}

worst_severity() {
  local worst="OK" agent
  for agent in "${AGENTS[@]}"; do worst="$(max_severity "$worst" "${OVERALL[$agent]:-OK}")"; done
  echo "$worst"
}

problem_agents_csv() {
  local names=() agent
  for agent in "${AGENTS[@]}"; do
    [ "${OVERALL[$agent]:-OK}" != "OK" ] && names+=("$agent")
  done
  [ "${#names[@]}" -eq 0 ] && { echo ""; return; }
  local IFS=', '; echo "${names[*]}"
}

# ── Notify state (anti-spam de-dupe) ─────────────────────────────────
# WHY: cron may fire often; re-sending the same problem every run is noise. We
# fingerprint the CURRENT problem set (stable issue keys, no timestamps) and only
# alert when it CHANGES (new/worse), after a cooldown reminder, or once/day for OK.
LAST_ALERT_HASH=""
LAST_PROBLEM_SIGNATURE=""
LAST_ALERT_TS=""
LAST_DAILY_OK_DATE=""

read_notify_state() {
  [ -f "$STATE_FILE" ] || return 0
  local line key val
  while IFS= read -r line || [ -n "$line" ]; do
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    [[ "$line" =~ = ]] || continue
    key="${line%%=*}"; val="${line#*=}"
    case "$key" in
      last_alert_hash) LAST_ALERT_HASH="$val" ;;
      last_problem_signature) LAST_PROBLEM_SIGNATURE="$val" ;;
      last_alert_ts) LAST_ALERT_TS="$val" ;;
      last_daily_ok_date) LAST_DAILY_OK_DATE="$val" ;;
    esac
  done < "$STATE_FILE"
}

write_notify_state() {
  local report_hash="$1" had_problems="$2" sent_kind="$3" problem_signature="$4"
  local today now_ts
  today="$(date +%Y-%m-%d)"; now_ts="$(date +%s)"
  mkdir -p "$(dirname "$STATE_FILE")"
  {
    if [ "$had_problems" = true ]; then
      echo "last_alert_hash=${report_hash}"
      echo "last_problem_signature=${problem_signature}"
      echo "last_alert_ts=${now_ts}"
      echo "last_daily_ok_date=${LAST_DAILY_OK_DATE}"
    else
      echo "last_alert_hash="
      echo "last_problem_signature="
      echo "last_alert_ts="
      echo "last_daily_ok_date=${today}"
    fi
  } > "$STATE_FILE"
}

build_problem_signature() {
  [ "${#PROBLEM_ISSUES[@]}" -eq 0 ] && { echo ""; return; }
  printf '%s\n' "${PROBLEM_ISSUES[@]}" | LC_ALL=C sort | paste -sd'|' -
}

hash_signature() { printf '%s' "$1" | (sha256sum 2>/dev/null || shasum -a 256) | awk '{print $1}'; }

within_alert_cooldown() {
  [ -z "$LAST_ALERT_TS" ] && return 1
  [[ "$LAST_ALERT_TS" =~ ^[0-9]+$ ]] || return 1
  [ "$(( $1 - LAST_ALERT_TS ))" -lt "$ALERT_COOLDOWN_SEC" ]
}

# ── Port probe (portable: ss → lsof → nc) ─────────────────────────────
# Linux usually has ss; macOS has lsof; nc is the last resort. Returns 0 if
# something is listening on the port.
_port_listening() {
  local port="$1"
  [ -z "$port" ] && return 1
  if command -v ss >/dev/null 2>&1; then
    ss -tln 2>/dev/null | grep -q ":${port} " && return 0
  fi
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1 && return 0
  fi
  if command -v nc >/dev/null 2>&1; then
    nc -z 127.0.0.1 "$port" >/dev/null 2>&1 && return 0
  fi
  return 1
}

# ── Gateway health (OS-detect, no hardcoded service/port) ─────────────────
# Returns: SEVERITY|USER_MESSAGE
# Resolution order per OS, first hit wins:
#   Linux : systemctl --user (dedicated unit > shared) > systemctl system > pgrep
#   macOS : launchctl list match > pgrep
#   other : pgrep only
# A dedicated per-agent unit (openclaw-gateway-<id>) is preferred when present,
# else the shared openclaw-gateway, else any openclaw process. Port is only
# probed when we can discover one; unknown port → process-liveness is enough
# (no false "not responding" on setups that don't expose a fixed port).
_systemd_unit_active() {
  local unit="$1"
  systemctl --user is-active --quiet "$unit" 2>/dev/null && return 0
  systemctl is-active --quiet "$unit" 2>/dev/null && return 0
  return 1
}

check_gateway() {
  local aid="$1"
  local up=false how=""

  if [ "$IS_MACOS" = true ]; then
    if command -v launchctl >/dev/null 2>&1 \
       && launchctl list 2>/dev/null | grep -qiE "openclaw.*(${aid}|gateway)"; then
      up=true; how="launchctl"
    fi
  elif command -v systemctl >/dev/null 2>&1; then
    if _systemd_unit_active "openclaw-gateway-$aid.service"; then
      up=true; how="systemd:$aid"
    elif _systemd_unit_active "openclaw-gateway.service"; then
      up=true; how="systemd:shared"
    fi
  fi

  # Fallback / cross-check: any openclaw process alive.
  if [ "$up" = false ] && command -v pgrep >/dev/null 2>&1; then
    if pgrep -f "openclaw" >/dev/null 2>&1; then
      up=true; how="process"
    fi
  fi

  if [ "$up" = true ]; then
    echo "OK|Gateway online ✅ (${how})"
  else
    echo "ERROR|Gateway offline ❌ — restart the OpenClaw gateway"
  fi
}

# ── DB health ────────────────────────────────────────────────────
# Returns: SEVERITY|USER_MESSAGE. Empty db path (agent has none discovered) is
# not an error — some setups keep memory elsewhere; we report a soft skip.
check_db() {
  local db_path="$1" resolved="$1"
  [ -z "$db_path" ] && { echo "OK|Database: (path not resolved, skipped)"; return; }
  if [ -L "$db_path" ]; then
    resolved="$(readlink -f "$db_path" 2>/dev/null || readlink "$db_path" 2>/dev/null || echo "$db_path")"
  fi
  if [ -z "$resolved" ] || [ ! -e "$resolved" ]; then
    echo "ERROR|Database not found ❌"; return
  fi
  if python3 - "$resolved" <<'PY'
import sqlite3, sys
con = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True, timeout=5)
con.execute("SELECT 1"); con.close()
PY
  then echo "OK|Database OK ✅"
  else echo "ERROR|Database unreadable ❌"
  fi
}

format_age_friendly() {
  local m="$1"
  if [ "$m" -lt 60 ]; then echo "${m} min"
  elif [ "$m" -lt 1440 ]; then echo "$(( m / 60 ))h"
  else echo "$(( m / 1440 ))d"
  fi
}

# ── Cron-log freshness (the 3 base crons) ───────────────────────────
# Returns: SEVERITY|USER_MESSAGE|ISSUE_CLASS(ok|missing|stale|error)
# Reads the log's newest timestamp (or file mtime) for staleness, and scans the
# tail for a failure signature guarded by a mid-run marker so a job sampled
# WHILE running isn't misread as failed.
check_log_job() {
  local logfile="$1" max_hours="$2" label="$3"
  if [ ! -f "$logfile" ]; then
    # A log that has NEVER been created means this agent does not run this cron
    # (crons are per-agent; not every agent runs memory_cleanup/memory_review).
    # That is not a fault — only a log that EXISTED and then went stale/errored is.
    # Report OK (skipped) instead of a permanent false "never ran" WARN.
    # (Fresh-install grace is handled separately by the caller when it applies.)
    echo "OK|${label}: (not run by this agent, skipped)|not_applicable"; return
  fi
  local max_min=$(( max_hours * 60 )) tail_chunk recent_chunk last_ts age_min age_from_log=-1
  tail_chunk="$(tail -n 40 "$logfile" 2>/dev/null || true)"
  recent_chunk="$(tail -n 8 "$logfile" 2>/dev/null || true)"
  last_ts="$(printf '%s\n' "$tail_chunk" | grep -oE '20[0-9]{2}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}' | tail -1 || true)"
  if [ -n "$last_ts" ]; then
    age_from_log=$(( ( $(date +%s) - $(date -d "$last_ts" +%s 2>/dev/null || echo 0) ) / 60 ))
  fi
  age_min=$(( ( $(date +%s) - $(stat -c %Y "$logfile" 2>/dev/null || stat -f %m "$logfile" 2>/dev/null || echo 0) ) / 60 ))
  [ "$age_from_log" -ge 0 ] && [ "$age_from_log" -lt "$age_min" ] && age_min="$age_from_log"

  local status="OK" issue_class="ok"
  # Failure only counts when the RECENT tail doesn't show a clean/in-progress
  # marker (mid-run guard: watcher can sample a heavy job mid-execution).
  if printf '%s\n' "$tail_chunk" | grep -qiE 'traceback|\[dinomem_run\] ERROR|operationalerror| exited 1|fatal error' \
     && ! printf '%s\n' "$recent_chunk" | grep -qiE 'completed successfully|✅|in progress|self-healing|lock released'; then
    status="ERROR"; issue_class="error"
  fi
  if [ "$age_min" -gt "$max_min" ]; then
    if [ "$status" = "OK" ]; then
      echo "WARN|${label}: no run in $(format_age_friendly "$age_min")|stale"; return
    fi
    issue_class="stale"
  elif [ "$status" = "OK" ]; then
    echo "OK||ok"; return
  fi
  echo "${status}|${label}: failing (last run $(format_age_friendly "$age_min") ago)|${issue_class}"
}

# ── VPS resource block (portable /proc + df, Linux; macOS best-effort) ────────
build_resource_block() {
  python3 - "$OC" "${AGENTS[@]}" <<'PY' 2>/dev/null || printf '%s' "💻 VPS resource — (unreadable)"
import os, sys, shutil, subprocess
oc = sys.argv[1]; agents = sys.argv[2:]
def _load1():
    try: return f"{os.getloadavg()[0]:.2f}"
    except Exception: return "?"
cores = os.cpu_count() or 1
load1 = _load1()
# RAM: /proc/meminfo (Linux) else `vm_stat`/sysctl (macOS best-effort skip).
mem_line = ""
try:
    mi = {}
    with open("/proc/meminfo") as f:
        for ln in f:
            k, _, v = ln.partition(":")
            mi[k] = int(v.strip().split()[0])  # kB
    total = mi.get("MemTotal", 0) // 1024
    avail = mi.get("MemAvailable", 0) // 1024
    used = total - avail
    pct = (used * 100 // total) if total else 0
    mem_line = f"{used}MB / {total}MB ({pct}%), {avail}MB free"
except Exception:
    mem_line = "(n/a on this OS)"
# Disk of OC mount (portable via shutil).
try:
    du = shutil.disk_usage(oc)
    disk_line = f"{du.used//2**20}MB / {du.total//2**20}MB ({du.used*100//du.total}%)"
except Exception:
    disk_line = "(n/a)"
# dinomem kb/ footprint across agents (one du pass; skip if du absent).
kb_paths = []
WSF = None
for a in agents:
    for cand in (f"{oc}/workspace-{a}", f"{oc}/workspace"):
        if os.path.isdir(f"{cand}/kb"):
            kb_paths.append(f"{cand}/kb"); break
kb_gb = "0.0"
if kb_paths and shutil.which("du"):
    try:
        r = subprocess.run(["du", "-sc", "-k", *kb_paths], capture_output=True, text=True, timeout=20)
        tot_k = int(r.stdout.strip().splitlines()[-1].split()[0])
        kb_gb = f"{tot_k/1024/1024:.1f}"
    except Exception: pass
warn = ""
try:
    if float(load1) > cores * 2: warn += " ⚠️load"
except Exception: pass
out = [f"💻 VPS resource{warn}"]
out.append(f"  • Load: {load1} / {cores} core")
out.append(f"  • RAM: {mem_line}")
out.append(f"  • Disk ({oc}): {disk_line}")
out.append(f"  • dinomem kb/ footprint: {kb_gb}GB ({len(kb_paths)} agent)")
print("\n".join(out))
PY
}

# ── Recall-activity block (base: reads kb/retrieval_log/<today>.jsonl) ────────
# WHY base-safe: base ships procedures/_retrieval_log.py which writes this exact
# per-day JSONL, so the block works WITHOUT the neuron upgrade. Agents with no
# log today are summarized as one quiet line.
build_recall_block() {
  python3 - "$OC" "${AGENTS[@]}" <<'PY' 2>/dev/null || printf '%s' "🧠 Recall today — (unreadable)"
import json, os, sys, datetime
oc = sys.argv[1]; agents = sys.argv[2:]
today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
def ws_of(a):
    for c in (f"{oc}/workspace-{a}", f"{oc}/workspace"):
        if os.path.isdir(c): return c
    return f"{oc}/workspace-{a}"
rows = []; idle = []; g_total = 0
for a in agents:
    f = f"{ws_of(a)}/kb/retrieval_log/{today}.jsonl"
    if not os.path.exists(f): idle.append(a); continue
    per = {}; n = 0; empty = 0
    with open(f) as fh:
        for ln in fh:
            ln = ln.strip()
            if not ln: continue
            try: r = json.loads(ln)
            except Exception: continue
            n += 1; t = r.get("tool", "?"); per[t] = per.get(t, 0) + 1
            if not r.get("n_results"): empty += 1
    if n == 0: idle.append(a); continue
    g_total += n; rows.append((a, n, empty, per))
# TIME GATE: the health cron typically runs early (07:00 local), when almost
# nobody has recalled yet, so "0× today / 0 active" is expected noise, not a
# signal. Before EARLY_HOUR (LOCAL wall-clock, default 12), when NO agent has
# recalled, collapse the section to one quiet line instead of listing every
# idle agent as if it were a fault. After that hour the full per-agent
# breakdown returns, so a genuinely low-recall day still surfaces. Uses local
# time (not the UTC 'today' above) so "morning" means morning in the box's zone.
EARLY_HOUR = int(os.environ.get("DINOMEM_RECALL_EARLY_HOUR", "12"))
if not rows and datetime.datetime.now().hour < EARLY_HOUR:
    print(f"🧠 Recall today ({today}) — early hours, no activity yet (normal)"); raise SystemExit
if not rows and not idle:
    print("🧠 Recall today — (no data)"); raise SystemExit
rows.sort(key=lambda r: r[1], reverse=True)
out = [f"🧠 Recall today ({today}) — TOTAL {g_total}× ({len(rows)} active)"]
for a, n, empty, per in rows:
    tb = ", ".join(f"{k}:{v}" for k, v in sorted(per.items(), key=lambda x: -x[1]))
    warn = f"  ⚠️{empty}/{n} empty" if (n >= 5 and empty * 100 // n >= 50) else ""
    out.append(f"  • {a}: {n}× [{tb}]{warn}")
if idle: out.append(f"  • (no recall today: {', '.join(idle)})")
print("\n".join(out))
PY
}

# ── Memory-integrity block (NEURON-GRACEFUL) ───────────────────────────
# Two zero-LLM signals. The contradiction leg needs procedures/contradiction_check.py
# (NEURON-ONLY) — if absent, that leg is SKIPPED silently and only the open-notes
# leg (base-safe: just counts _note_ files) runs. All-clean → one quiet OK line.
build_memory_integrity_block() {
  python3 - "$OC" "${AGENTS[@]}" <<'PY' 2>/dev/null || printf '%s' "🩺 Memory integrity — (unreadable)"
import os, re, sys, subprocess
oc = sys.argv[1]; agents = sys.argv[2:]
OPEN_NOTES_WARN = int(os.environ.get("DINOMEM_OPEN_NOTES_WARN", "8"))
def ws_of(a):
    for c in (f"{oc}/workspace-{a}", f"{oc}/workspace"):
        if os.path.isdir(c): return c
    return None
flagged = []; checked = 0; neuron_seen = False
for a in agents:
    ws = ws_of(a)
    if not ws: continue
    checked += 1; issues = []
    # 1. contradiction leg — NEURON-ONLY, skip cleanly if the script is absent.
    cc = f"{ws}/procedures/contradiction_check.py"
    if os.path.exists(cc):
        neuron_seen = True
        try:
            r = subprocess.run(["python3", cc, "--report"], cwd=ws,
                               capture_output=True, text=True, timeout=25)
            m = re.search(r'Blocked insights:\s*(\d+)', r.stdout)
            if m and int(m.group(1)) > 0: issues.append(f"{m.group(1)} conflicting insight")
        except Exception: pass
    # 2. open-notes leg — base-safe.
    mem = f"{ws}/memory"; op = 0
    if os.path.isdir(mem):
        for fn in os.listdir(mem):
            if fn.startswith("_note_") and fn.endswith(".md"):
                try: txt = open(f"{mem}/{fn}", encoding="utf-8").read(2000)
                except Exception: continue
                mm = re.search(r'status:\s*(\w+)', txt)
                if mm and mm.group(1).lower() in ("in_progress", "pending"): op += 1
    if op > OPEN_NOTES_WARN: issues.append(f"{op} notes stuck")
    if issues: flagged.append((a, issues))
if checked == 0:
    print("🩺 Memory integrity — (no data)"); raise SystemExit
if not flagged:
    tail = "" if neuron_seen else " (open-notes only; neuron not installed)"
    print(f"🩺 Memory integrity — OK ({checked} agent clean){tail}"); raise SystemExit
out = [f"🩺 Memory integrity — ⚠️ {len(flagged)} agent to check"]
for a, issues in flagged: out.append(f"  • {a}: {', '.join(issues)}")
print("\n".join(out))
PY
}

# ── Report assembly ───────────────────────────────────────────────
_common_sections() {
  printf '%s\n\n%s\n\n%s' \
    "$(build_resource_block)" "$(build_recall_block)" "$(build_memory_integrity_block)"
}

build_problem_message() {
  local worst problem_agents header message agent
  worst="$(worst_severity)"; problem_agents="$(problem_agents_csv)"
  if [ "$worst" = "ERROR" ]; then
    header="🚨 dinomem — ${problem_agents:-infrastructure} problem"
  else
    header="⚠️ dinomem — ${problem_agents:-infrastructure} needs a look"
  fi
  message="$header"
  for agent in "${AGENTS[@]}"; do
    [ "${OVERALL[$agent]:-OK}" != "OK" ] && message+=$'\n\n'"${REPORT_BLOCKS[$agent]}"
  done
  message+=$'\n\n'"$(_common_sections)"
  printf '%s' "$message"
}

build_ok_message() {
  printf '✅ dinomem OK — %s\nAll %s agent(s) healthy.\n\n%s' \
    "$(date +%Y-%m-%d)" "${#AGENTS[@]}" "$(_common_sections)"
}

# ── Telegram delivery (owner choice B → A) ───────────────────────────
# (B) Try Telegram FIRST when a bot token + chat id are resolvable from
# openclaw.json (or env). (A) On ANY gap — no config, no token, no chat, send
# failure — auto-fall back to stdout + log. Returns 0 always (never crash the run).
# Prints the report to stdout on the fallback path so cron can mail it.
_telegram_send() {
  local message="$1"
  python3 - "$TELEGRAM_CONFIG" "$TELEGRAM_ACCOUNT" "$TELEGRAM_CHAT_ID" "$TELEGRAM_TOPIC_ID" "$message" <<'PY'
import json, sys, urllib.parse, urllib.request
cfg_path, account, chat_id, topic_id, message = sys.argv[1:6]
try:
    with open(cfg_path, encoding="utf-8") as f: cfg = json.load(f)
except Exception:
    sys.exit(3)  # no/unreadable config -> fall back
accounts = (cfg.get("channels", {}).get("telegram", {}).get("accounts", {}) or {})
# Resolve account: explicit env pick, else the first telegram account present.
if account and account in accounts:
    acct = accounts[account]
elif accounts:
    acct = next(iter(accounts.values()))
else:
    sys.exit(3)  # telegram not configured -> fall back
token = acct.get("botToken") or acct.get("token")
if not token:
    sys.exit(3)  # no token -> fall back
if not chat_id:
    # try a configured default chat on the account, else fall back
    chat_id = acct.get("defaultChatId") or acct.get("chatId") or ""
if not chat_id:
    sys.exit(4)  # token ok but nowhere to send -> fall back
data = {"chat_id": chat_id, "text": message, "disable_web_page_preview": "true"}
if topic_id: data["message_thread_id"] = topic_id
payload = urllib.parse.urlencode(data).encode()
try:
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=payload, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode())
    if not body.get("ok"):
        sys.exit(5)
except Exception:
    sys.exit(5)  # API/network error -> fall back
print(f"telegram ok message_id={body.get('result', {}).get('message_id')}")
PY
}

deliver() {
  local message="$1"
  if [ "$DRY_RUN" = true ]; then
    log "DRY-RUN: would deliver report (${#message} chars)" >>"$LOG_FILE"
    printf '%s\n' "$message"
    return 0
  fi
  if [ "$FORCE_STDOUT" != true ] && _telegram_send "$message" >>"$LOG_FILE" 2>&1; then
    log "Report sent to Telegram" >>"$LOG_FILE"
    return 0
  fi
  # (A) Fallback: stdout + log so cron/mail captures it.
  [ "$FORCE_STDOUT" = true ] || log "Telegram unavailable — falling back to stdout" >>"$LOG_FILE"
  printf '%s\n' "$message"
  printf '%s\n' "$message" >>"$LOG_FILE"
  return 0
}

# ── Notify decision ────────────────────────────────────────────
decide() {
  local report_hash="$1" now_ts today
  read_notify_state
  today="$(date +%Y-%m-%d)"; now_ts="$(date +%s)"
  if [ "$FORCE" = true ] || [ "$NOTIFY_MODE" = "always" ]; then
    has_problems && echo "send|problem" || echo "send|ok"; return
  fi
  if has_problems; then
    if [ "$report_hash" = "$LAST_ALERT_HASH" ] && [ -n "$LAST_ALERT_HASH" ]; then
      within_alert_cooldown "$now_ts" && { echo "skip|unchanged"; return; }
    fi
    echo "send|problem"; return
  fi
  if [ "$LAST_DAILY_OK_DATE" = "$today" ]; then
    [ -n "$LAST_ALERT_HASH" ] && { echo "send|ok"; return; }  # recovered
    echo "skip|daily_ok_done"; return
  fi
  echo "send|ok"
}

# ── Main ──────────────────────────────────────────────────
mkdir -p "$(dirname "$LOG_FILE")"
log "Starting health check (dry_run=$DRY_RUN mode=$NOTIFY_MODE force=$FORCE agents=${#AGENTS[@]})" >>"$LOG_FILE"

if [ "${#AGENTS[@]}" -eq 0 ]; then
  log "No dinomem agents discovered under $OC — nothing to check" >>"$LOG_FILE"
  [ "$DRY_RUN" = true ] && echo "No dinomem agents discovered under $OC"
  exit 0
fi

# Per-agent checks (gateway + db + the 3 base crons). Staleness windows: 15-min
# job -> 2h; daily jobs -> 30h (6h grace past the 24h cycle so only a genuinely
# skipped day trips it).
for agent in "${AGENTS[@]}"; do
  ws="${AGENT_WS[$agent]}"
  sev="OK"; problems=()
  IFS='|' read -r gw_sev gw_msg <<< "$(check_gateway "$agent")"
  sev="$(max_severity "$sev" "$gw_sev")"
  [ "$gw_sev" != "OK" ] && { problems+=("$gw_msg"); record_problem_issue "${agent}:gateway:${gw_sev}"; }
  IFS='|' read -r db_sev db_msg <<< "$(check_db "${AGENT_DB[$agent]:-}")"
  sev="$(max_severity "$sev" "$db_sev")"
  [ "$db_sev" != "OK" ] && { problems+=("$db_msg"); record_problem_issue "${agent}:db:${db_sev}"; }
  # NOTE: auto_session_reset is NOT health-checked. It is USAGE-DRIVEN (soft run
  # on compaction >2, hard >5) and does not run at all on a day the agent is idle.
  # A stale/absent auto_reset log therefore means "agent wasn't used", not "cron
  # broke" — flagging it is a pure false positive. Removed 2026-09-07.
  for job in "memory_cleanup|$ws/logs/memory_cleanup.log|30" \
             "memory_review|$ws/logs/memory_review.log|30"; do
    IFS='|' read -r jkey jlog jhrs <<< "$job"
    IFS='|' read -r c_sev c_msg c_cls <<< "$(check_log_job "$jlog" "$jhrs" "$jkey")"
    sev="$(max_severity "$sev" "$c_sev")"
    [ "$c_sev" != "OK" ] && { problems+=("$c_msg"); record_problem_issue "${agent}:${jkey}:${c_sev}:${c_cls}"; }
  done
  OVERALL[$agent]="$sev"
  block="$(agent_status_emoji "$sev") ${agent} — $([ "$sev" = OK ] && echo 'all normal' || echo 'needs a look')"
  if [ "$sev" = "OK" ]; then
    block+=$'\n'"  All cron jobs running normally"
  else
    for p in "${problems[@]}"; do block+=$'\n'"  • ${p}"; done
  fi
  REPORT_BLOCKS[$agent]="$block"
  log "${agent}: ${sev}" >>"$LOG_FILE"
done

problem_signature="$(build_problem_signature)"
report_hash="$(hash_signature "$problem_signature")"
IFS='|' read -r action kind <<< "$(decide "$report_hash")"

if [ "$action" = "skip" ]; then
  log "Skipping notify: ${kind} (hash=${report_hash:0:12})" >>"$LOG_FILE"
  exit 0
fi

if [ "$kind" = "problem" ]; then message="$(build_problem_message)"; else message="$(build_ok_message)"; fi
deliver "$message"
if has_problems; then
  write_notify_state "$report_hash" true "$kind" "$problem_signature"
else
  write_notify_state "$report_hash" false "$kind" "$problem_signature"
fi
log "Health check complete (${kind})" >>"$LOG_FILE"
exit 0
