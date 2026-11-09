#!/usr/bin/env bash
# shellcheck disable=SC2034  # SVC_DOWN/SVC_URL are read by heal_services in lib/watcher_heal.sh
# watcher_checks.sh — extra per-agent + shared checks for dinomem-health-watcher.sh.
#
# Sourced, never executed. Every check prints ONE line "SEV|message|class" where
# SEV is OK | WARN | ERROR | SKIP. SKIP = check does not apply to this agent
# (feature not installed) and must never surface as a problem.
#
# WHY these live here: they were born in a box-specific fork of the watcher and
# proved useful on every dinomem/neuron install (graph split-brain, stale MEMORY.md
# index, retrieval rollup quality, cron-gate errors). Folding them back keeps ONE
# watcher instead of two drifting copies.
#
# Expects from the caller: format_age_friendly <minutes>, OC (state dir).
# Tunables (env): DINOMEM_GRAPH_MAX_AGE_H (36), DINOMEM_GRAPH_DB_JSON_MAX_SKEW_SEC (7200),
#   DINOMEM_MEMORY_MD_MAX_AGE_DAYS (7), DINOMEM_HEALTH_JSON_MAX_AGE_H (36),
#   DINOMEM_RETRIEVAL_EMPTY_RATE_WARN (0.5), DINOMEM_RETRIEVAL_FALLBACK_RATE_WARN (0.5),
#   DINOMEM_RETRIEVAL_RATE_MIN_CALLS (5), DINOMEM_WATCHER_CRON_GATE (1 = on).

_mtime() { stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null || echo 0; }

# Neuron memory graph artifact: missing or older than N hours. Base installs
# (no procedures/memory_graph.py) -> SKIP.
check_memory_graph_artifact() {
  local ws="$1"
  local graph_json="$ws/kb/memory_neuron/l2_graph/memory_graph.json"
  [ -f "$ws/procedures/memory_graph.py" ] || { echo "SKIP||na"; return; }
  [ -f "$graph_json" ] || { echo "WARN|memory_graph: memory_graph.json missing|missing"; return; }
  local max_min=$(( ${DINOMEM_GRAPH_MAX_AGE_H:-36} * 60 ))
  local age_min=$(( ($(date +%s) - $(_mtime "$graph_json")) / 60 ))
  [ "$age_min" -le "$max_min" ] && { echo "OK||ok"; return; }
  # An old artifact is NORMAL when memory_graph.py ran recently and skipped the
  # rebuild because the indexed memory content was unchanged (signature match).
  # Only an old artifact WITHOUT a recent "unchanged" run is a real stall.
  local glog="$ws/logs/memory_graph.log"
  if [ -f "$glog" ] && [ $(( ($(date +%s) - $(_mtime "$glog")) / 60 )) -le "$max_min" ] \
     && tail -n 6 "$glog" 2>/dev/null | grep -q 'memory unchanged'; then
    echo "OK||ok"; return
  fi
  echo "WARN|memory_graph: artifact is $(format_age_friendly "$age_min") old and no recent unchanged-skip run|stale"
}

# OpenClaw's memory index (memory_index_sources in the agent sqlite) must cover
# memory/*.md. The builtin file watcher can silently miss files (seen: 4 files
# un-indexed for 2-4 weeks while status said dirty=false), and then the graph
# job keeps skipping as "unchanged" forever. Files younger than the grace window
# are ignored (normal debounce / not-yet-synced). Fix: openclaw memory index --agent <id>.
check_memory_index_lag() {
  local ws="$1" db="$2"
  [ -n "$db" ] && [ -f "$db" ] && [ -d "$ws/memory" ] || { echo "SKIP||na"; return; }
  python3 - "$ws" "$db" "${DINOMEM_INDEX_LAG_GRACE_H:-24}" <<'PY' 2>/dev/null || echo "SKIP||na"
import glob, os, sqlite3, sys, time
ws, db, grace_h = sys.argv[1], sys.argv[2], float(sys.argv[3])
con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
if not con.execute("select 1 from sqlite_master where name='memory_index_sources'").fetchone():
    print("SKIP||na"); sys.exit()
indexed = {r[0] for r in con.execute("select path from memory_index_sources")}
cutoff = time.time() - grace_h * 3600
lag = sorted(os.path.basename(f) for f in glob.glob(os.path.join(ws, "memory", "*.md"))
             if os.path.getmtime(f) < cutoff and os.path.relpath(f, ws) not in indexed)
if lag:
    print(f"WARN|memory_index: {len(lag)} memory file(s) not indexed (e.g. {lag[0]}); run openclaw memory index|index_lag")
else:
    print("OK||ok")
PY
}

# graph.db and memory_graph.json are written by the same job; a large mtime gap
# means one side stopped updating (split brain -> recall reads stale edges).
check_graph_db_json_sync() {
  local ws="$1" d="$1/kb/memory_neuron/l2_graph"
  [ -f "$d/memory_graph.json" ] && [ -f "$d/graph.db" ] || { echo "SKIP||na"; return; }
  local a b skew
  a=$(_mtime "$d/memory_graph.json"); b=$(_mtime "$d/graph.db")
  skew=$(( a > b ? a - b : b - a ))
  if [ "$skew" -gt "${DINOMEM_GRAPH_DB_JSON_MAX_SKEW_SEC:-7200}" ]; then
    echo "WARN|memory_graph: graph.db vs memory_graph.json out of sync (~$(( skew / 3600 ))h)|split_brain"; return
  fi
  echo "OK||ok"
}

# MEMORY.md is the always-loaded index. If memory/*.md keeps growing while the
# index has not been rewritten for N days, new facts are invisible at bootstrap.
check_memory_index_freshness() {
  local ws="$1" mem_md="$1/MEMORY.md" mem_dir="$1/memory"
  local max_days="${DINOMEM_MEMORY_MD_MAX_AGE_DAYS:-7}" now md_ts newest=0
  [ -f "$mem_md" ] || { echo "SKIP||na"; return; }
  now=$(date +%s); md_ts=$(_mtime "$mem_md")
  [ $(( (now - md_ts) / 86400 )) -lt "$max_days" ] && { echo "OK||ok"; return; }
  if [ -d "$mem_dir" ]; then
    newest="$(find "$mem_dir" -maxdepth 1 -type f -name '*.md' ! -name '_note_*' -printf '%T@\n' 2>/dev/null \
      | sort -n | tail -1 | cut -d. -f1)"
    newest="${newest:-0}"
  fi
  # A recent successful topic_index run counts as fresh even if it chose not to rewrite.
  local tl="$ws/logs/topic_index.log"
  if [ -f "$tl" ] && [ "$(_mtime "$tl")" -gt $(( now - max_days * 86400 )) ] \
     && tail -8 "$tl" 2>/dev/null | grep -qE 'Wrote .*MEMORY\.md|✅ Wrote'; then
    echo "OK||ok"; return
  fi
  if [ "$newest" -gt "$md_ts" ]; then
    echo "WARN|memory_index: MEMORY.md $(format_age_friendly $(( (now - md_ts) / 60 ))) behind memory/*.md (check promote/topic index)|stale_index"; return
  fi
  local pl="$ws/logs/memory_promote.log"
  if [ -f "$pl" ] && tail -6 "$pl" 2>/dev/null | grep -qiE 'no synthesis handoff|run memory_synthesis first'; then
    echo "WARN|memory_index: promote blocked (L3 synthesis not ready)|warn"; return
  fi
  echo "OK||ok"
}

# Retrieval quality rollup (kb/retrieval_log/health.json): stale snapshot, or a
# tool returning mostly empty / falling back. Quality, not liveness — WARN only.
check_retrieval_health() {
  local ws="$1" health="$1/kb/retrieval_log/health.json"
  [ -f "$ws/procedures/_retrieval_log.py" ] || { echo "SKIP||na"; return; }
  [ -f "$health" ] || { echo "WARN|retrieval_health: health.json missing (rollup never ran)|missing"; return; }
  python3 - "$health" "${DINOMEM_HEALTH_JSON_MAX_AGE_H:-36}" <<'PY'
import json, os, sys
from datetime import datetime, timezone
path, max_h = sys.argv[1], float(sys.argv[2])
env = os.environ.get
empty_w = float(env("DINOMEM_RETRIEVAL_EMPTY_RATE_WARN", "0.5"))
fb_w = float(env("DINOMEM_RETRIEVAL_FALLBACK_RATE_WARN", "0.5"))
min_calls = int(env("DINOMEM_RETRIEVAL_RATE_MIN_CALLS", "5"))
try:
    data = json.load(open(path, encoding="utf-8"))
except Exception:
    print("WARN|retrieval_health: health.json unreadable|warn"); sys.exit()
gen = str(data.get("generated_at") or "").strip()
try:
    ts = datetime.fromisoformat(gen[:-1] + "+00:00" if gen.endswith("Z") else gen)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
except Exception:
    print("WARN|retrieval_health: generated_at missing|warn"); sys.exit()
age_h = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
if age_h > max_h:
    print(f"WARN|retrieval_health: snapshot {int(age_h)}h old|stale"); sys.exit()
issues = []
tools = data.get("tools") or {}
items = tools.items() if isinstance(tools, dict) else ((t.get("tool", "?"), t) for t in tools if isinstance(t, dict))
for name, t in items:
    if int(t.get("calls") or 0) < min_calls:
        continue
    er, fr = float(t.get("empty_rate") or 0), float(t.get("fallback_rate") or 0)
    if er >= empty_w: issues.append(f"{name} empty {er*100:.0f}%")
    if fr >= fb_w: issues.append(f"{name} fallback {fr*100:.0f}%")
if issues:
    more = f" (+{len(issues)-4} more)" if len(issues) > 4 else ""
    print("WARN|retrieval_health: " + ", ".join(issues[:4]) + more + "|quality")
else:
    print("OK||ok")
PY
}

# Cron-gate jobs in `openclaw cron list` reporting error status. The listing is
# expensive (~13s, ~280MB RSS) and identical for every agent sharing a state dir,
# so it is cached per state dir for 30 min in a file (checks run in subshells).
check_cron_gate() {
  local agent="$1" ws="$2" state_dir="${3:-$OC}"
  case "${DINOMEM_WATCHER_CRON_GATE:-1}" in 0|false|no|off) echo "SKIP||na"; return ;; esac
  [ -f "$ws/scripts/cron_gate.sh" ] || { echo "SKIP||na"; return; }
  command -v openclaw >/dev/null 2>&1 || { echo "SKIP||na"; return; }
  local key cache out lines
  key="$(printf '%s' "$state_dir" | md5sum 2>/dev/null | cut -c1-12)"
  cache="${TMPDIR:-/tmp}/dinomem-health-cronlist-${key:-default}.txt"
  if [ -s "$cache" ] && [ -n "$(find "$cache" -mmin -30 2>/dev/null)" ]; then
    out="$(cat "$cache")"
  else
    out="$(OPENCLAW_STATE_DIR="$state_dir" timeout 120 openclaw cron list 2>/dev/null || true)"
    [ -n "$out" ] && ( umask 077; printf '%s\n' "$out" > "$cache.$$" ) && mv -f "$cache.$$" "$cache"
  fi
  lines="$(printf '%s\n' "$out" | grep -i 'cron gate' | grep -i -- "$agent" || true)"
  [ -n "$lines" ] || { echo "OK||ok"; return; }
  # Match status tokens like "error (3x)", not delivery hints such as "fail-closed".
  if printf '%s\n' "$lines" | grep -qiE 'error \([0-9]+x\)|[[:space:]]error[[:space:]]'; then
    echo "WARN|cron_gate: job in error state (openclaw cron list)|warn"; return
  fi
  echo "OK||ok"
}

# ── Extra services slot (box-local, config-driven) ───────────────────────────
# DINOMEM_WATCHER_SERVICES: newline- or ';'-separated "name|health_url|restart_cmd".
# The repo knows nothing about what these are (a model router, redis, nginx...);
# the box config names them. restart_cmd is optional; empty = report only.
# Filled: SVC_DOWN[name]=restart_cmd for every unhealthy entry (used by heal).
declare -A SVC_DOWN=() SVC_URL=()
check_extra_services() {
  local spec="${DINOMEM_WATCHER_SERVICES:-}" entry name url cmd
  [ -n "$spec" ] || return 0
  while IFS= read -r entry; do
    entry="${entry#"${entry%%[![:space:]]*}"}"; [ -n "$entry" ] || continue
    IFS='|' read -r name url cmd <<< "$entry"
    [ -n "$name" ] && [ -n "$url" ] || continue
    if curl -fsS -m "${DINOMEM_WATCHER_SERVICE_TIMEOUT:-5}" -o /dev/null "$url" 2>/dev/null; then
      echo "OK|${name}: healthy|ok|${name}"
    else
      SVC_DOWN[$name]="$cmd"; SVC_URL[$name]="$url"
      echo "ERROR|${name}: health check failed ($url)|down|${name}"
    fi
  done <<< "$(printf '%s' "$spec" | tr ';' '\n')"
}
