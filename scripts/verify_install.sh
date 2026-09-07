#!/usr/bin/env bash
# verify_install.sh — post-install completeness self-check for dinomem (base + neuron).  [v1]
#
# WHY THIS EXISTS (the incident that motivated it):
#   An operator once believed an install was "incomplete" because the dinomem
#   crons were absent from `openclaw cron list`. They were NOT missing — they
#   were all present the whole time. The confusion: dinomem's recurring jobs are
#   registered by upsert_cron() into the SYSTEM CRONTAB (`crontab -l`), NOT into
#   the Gateway's SQLite cron store that `openclaw cron list` reads. Two
#   different schedulers. Querying the wrong one read "absent" as "broken" and
#   triggered a long phantom investigation.
#
#   This script closes that gap: it checks the RIGHT surface (the crontab) and
#   prints a single machine-readable checklist so "is this install complete?"
#   is answerable in seconds, not forensics.
#
# CONTRACT (stable):
#   - Human-readable checklist to stdout, one PASS/FAIL/WARN line per check.
#   - LAST stdout line is ALWAYS exactly one of:
#       VERIFY_INSTALL: OK <layer> (<n> checks)
#       VERIFY_INSTALL: FAIL <layer> :: <n> failure(s): <first-failure>
#   - exit 0 when every REQUIRED check passes (WARNs do not fail), exit 1 on any
#     required FAIL, exit 2 on usage error.
#   - <layer> = "base" or "base+neuron", auto-detected from AGENTS.md markers.
#
# USAGE:
#   bash scripts/verify_install.sh [--workspace <WS>]
#   (WS defaults to $OPENCLAW_WORKSPACE, then $HOME/.openclaw/workspace)

set -uo pipefail 2>/dev/null || true

# ── args ──────────────────────────────────────────────────────────────────────
WS="${OPENCLAW_WORKSPACE:-$HOME/.openclaw/workspace}"
while [ $# -gt 0 ]; do
  case "$1" in
    --workspace) WS="${2:-}"; shift 2 ;;
    -h|--help)
      echo "usage: verify_install.sh [--workspace <WS>]"; exit 2 ;;
    *) echo "verify_install.sh: unknown arg '$1'" >&2
       echo "usage: verify_install.sh [--workspace <WS>]"; exit 2 ;;
  esac
done

if [ -z "$WS" ] || [ ! -d "$WS" ]; then
  echo "VERIFY_INSTALL: FAIL unknown :: 1 failure(s): workspace not found: '$WS'"
  exit 1
fi
WS="$(cd "$WS" && pwd)"   # normalize

# ── tiny check harness ─────────────────────────────────────────────────────────
FAILS=0; CHECKS=0; FIRST_FAIL=""
_pass() { CHECKS=$((CHECKS+1)); printf '  PASS  %s\n' "$1"; }
_warn() {              printf '  WARN  %s\n' "$1"; }   # advisory, never fails
_fail() { CHECKS=$((CHECKS+1)); FAILS=$((FAILS+1))
          [ -z "$FIRST_FAIL" ] && FIRST_FAIL="$1"
          printf '  FAIL  %s\n' "$1"; }

# ── layer detection ────────────────────────────────────────────────────────────
# neuron is present iff AGENTS.md carries a COMPLETE neuron block span.
LAYER="base"
if [ -f "$WS/AGENTS.md" ] \
   && grep -qF 'BEGIN:dinomem-neuron' "$WS/AGENTS.md" 2>/dev/null \
   && grep -qF 'END:dinomem-neuron'   "$WS/AGENTS.md" 2>/dev/null; then
  LAYER="base+neuron"
fi

echo "dinomem install verify — workspace=$WS  layer=$LAYER"
echo "(crons checked against the SYSTEM CRONTAB — the surface upsert_cron writes to,"
echo " NOT 'openclaw cron list' which reads the separate Gateway SQLite store)"
echo

# ── 1. AGENTS.md managed blocks ────────────────────────────────────────────────
if [ -f "$WS/AGENTS.md" ]; then
  grep -qF 'BEGIN:dinomem'  "$WS/AGENTS.md" && grep -qF 'END:dinomem' "$WS/AGENTS.md" \
    && _pass "AGENTS.md base block present" \
    || _fail "AGENTS.md base block missing/incomplete"
  if [ "$LAYER" = "base+neuron" ]; then
    _pass "AGENTS.md neuron block present"
  fi
else
  _fail "AGENTS.md not found at $WS/AGENTS.md"
fi

# ── 2. crons in the SYSTEM CRONTAB (the whole point of this script) ────────────
# Expected keyword per required cron, matched against the crontab line that
# upsert_cron writes. Keep in lockstep with install.sh upsert_cron calls.
CRONTAB="$(crontab -l 2>/dev/null || true)"
# scope crontab lines to THIS workspace so multi-agent boxes don't cross-count
WS_CRONS="$(printf '%s\n' "$CRONTAB" | grep -F "$WS" || true)"

base_crons="auto_session_reset.py memory_cleanup.py memory_review.py cleanup_startup_daily.py"
# workspace_backup + weekly_stats are opt-in-able; treat as WARN if absent.
base_warn_crons="workspace_backup.py weekly_stats.py"
neuron_crons="memory_graph.py memory_synthesis.py memory_promote.py code_graph.py _retrieval_log.py"

for kw in $base_crons; do
  printf '%s\n' "$WS_CRONS" | grep -qF "$kw" \
    && _pass "crontab: $kw registered" \
    || _fail "crontab: $kw MISSING (re-run installer, or check 'crontab -l')"
done
for kw in $base_warn_crons; do
  printf '%s\n' "$WS_CRONS" | grep -qF "$kw" \
    && _pass "crontab: $kw registered" \
    || _warn "crontab: $kw absent (opt-in cron; fine if you used --no-backup-cron etc.)"
done
if [ "$LAYER" = "base+neuron" ]; then
  for kw in $neuron_crons; do
    printf '%s\n' "$WS_CRONS" | grep -qF "$kw" \
      && _pass "crontab: $kw registered (neuron)" \
      || _fail "crontab: $kw MISSING (neuron re-run needed)"
  done
fi

# ── 3. Note Cron Gate (the zero-LLM dispatcher) ────────────────────────────────
# Lives in the Gateway cron store (command-kind), NOT the crontab — so THIS one
# is the exception where `openclaw cron list` is the right place to look.
if command -v openclaw >/dev/null 2>&1; then
  if openclaw cron list 2>/dev/null | grep -qi 'Note Cron Gate'; then
    _pass "Gateway cron: 'Note Cron Gate' present (zero-LLM dispatcher)"
  else
    _warn "Gateway cron: 'Note Cron Gate' not found — note-janitor may be on fallback per-worker schedules (check installer output)"
  fi
else
  _warn "openclaw CLI not on PATH — skipped 'Note Cron Gate' check"
fi

# ── 4. core procedures on disk ─────────────────────────────────────────────────
proc_base="auto_session_reset.py memory_cleanup.py memory_review.py cleanup_startup_daily.py"
proc_neuron="memory_graph.py memory_synthesis.py contradiction_check.py confidence_engine.py memory_promote.py code_graph.py"
for p in $proc_base; do
  [ -f "$WS/procedures/$p" ] && _pass "procedure present: procedures/$p" \
                             || _fail "procedure MISSING: procedures/$p"
done
if [ "$LAYER" = "base+neuron" ]; then
  for p in $proc_neuron; do
    [ -f "$WS/procedures/$p" ] && _pass "procedure present: procedures/$p" \
                               || _fail "procedure MISSING: procedures/$p (neuron)"
  done
fi

# ── 5. scripts the crons invoke ────────────────────────────────────────────────
for s in cron_gate.sh dinomem_run.sh; do
  [ -f "$WS/scripts/$s" ] && _pass "script present: scripts/$s" \
                          || _fail "script MISSING: scripts/$s"
done

# ── 6. neuron data-store dirs (RAG/graph/code — created lazily but expected) ────
if [ "$LAYER" = "base+neuron" ]; then
  for d in kb/vector_db kb/memory_neuron; do
    [ -d "$WS/$d" ] && _pass "data dir present: $d" \
                    || _warn "data dir absent: $d (created on first cron run — fine on a fresh box)"
  done
fi

# ── 7. openclaw.json validity (a broken config crash-loops the Gateway) ────────
if command -v openclaw >/dev/null 2>&1; then
  if openclaw config validate >/dev/null 2>&1; then
    _pass "openclaw config validate: clean"
  else
    _fail "openclaw config validate: FAILED (config would crash-loop the Gateway)"
  fi
else
  _warn "openclaw CLI not on PATH — skipped config validate"
fi

# ── verdict ────────────────────────────────────────────────────────────────────
echo
if [ "$FAILS" -eq 0 ]; then
  echo "VERIFY_INSTALL: OK $LAYER ($CHECKS checks)"
  exit 0
else
  echo "VERIFY_INSTALL: FAIL $LAYER :: $FAILS failure(s): $FIRST_FAIL"
  exit 1
fi
