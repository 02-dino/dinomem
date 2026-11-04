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

# ── 6b. skill TRIGGERS in AGENTS.md (orphan-skill check) ──────────────────────
# WHY (2026-10-18): shipping a skill is only half an install — a skill with no
# `skills:` trigger line in the managed AGENTS.md block is UNREACHABLE. It sits on
# disk looking installed and is never read, because nothing tells the agent when to
# open it. Measured: edit-verify-loop and openclaw-config-edit were both shipped and
# installed but orphaned for months; openclaw-config-edit was reachable ONLY from
# subagent_build_discipline, so a MAIN session editing openclaw.json got no trigger
# at all. This check exists so skill #11 cannot silently repeat that.
#
# TRIGGER CONVENTION (what counts): the managed block writes triggers as
#   <key>: <when...> → read skill "<skill-dir-name>"
# so the QUOTED name is the discriminator. An unquoted prose mention (e.g.
# "...edit-verify-loop is the post-edit syntax-gate half") is a cross-reference,
# NOT a trigger — which is exactly the orphan state we are hunting. Matching on the
# quoted form is what makes "mentioned" vs "triggered" distinguishable.
#
# SOURCE OF TRUTH (two run modes): from a CHECKOUT, enumerate the repo's own
# skills/ dir so a newly added skill is covered with zero edits here. From an
# INSTALLED workspace, scripts/ and skills/ both live under $WS and skills/ is mixed
# dinomem + the agent's own custom skills — enumerating there would flag every
# custom skill as an orphan. So that mode falls back to the shipped list, same
# hardcoded-roster convention this file already uses for crons/procedures.
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd || echo '')"
skills_shipped="backup-restore build-quality cron-config edit-verify-loop hook-config memory-pinning openclaw-config-edit safe-file-ops self-config skill-config"

# NOT EVERY SKILL IS REACHED THROUGH AGENTS.md. A skill whose ONLY caller is a
# cron payload (the job prompt names skills/<x>/SKILL.md directly) is reached
# without any AGENTS.md trigger, so judging it by the AGENTS.md rule reports a
# healthy install as broken. Its real liveness test is "does the cron exist",
# which is what we check below instead.
#
# WHY THIS SET HAD TO BECOME EXPLICIT (measured 2026-11-04): the roster above
# already omits auto-improve on purpose and matches the 10 triggers the
# installer writes — but the checkout branch below REPLACES the roster with
# `ls skills/`, which re-adds it. Same script, two run modes, opposite verdicts:
# from an installed workspace auto-improve was silently (and correctly) skipped,
# from a checkout it was flagged "it will never be opened" while its cron was
# sitting right there. The intent lived only in a hand-maintained list, so the
# other mode could not honour it. Naming the set fixes both modes at once.
skills_cron_driven="auto-improve"

if [ -n "$SELF_DIR" ] && [ "$SELF_DIR" != "$WS" ] && [ -f "$SELF_DIR/VERSION" ] && [ -d "$SELF_DIR/skills" ]; then
  skills_shipped="$(cd "$SELF_DIR/skills" && ls -d */ 2>/dev/null | tr -d '/' | tr '\n' ' ')"
fi
if [ -f "$WS/AGENTS.md" ]; then
  _orphans=0
  for sk in $skills_shipped; do
    # Only judge skills actually INSTALLED here — an opt-out/partial install must
    # not be reported as an orphaned trigger.
    [ -f "$WS/skills/$sk/SKILL.md" ] || continue

    # Cron-driven skill: an AGENTS.md trigger is not expected. Report on the
    # door it ACTUALLY has. A missing cron is a WARN, not a FAIL: the gateway
    # cron store is a separate surface and may legitimately be unreachable here.
    case " $skills_cron_driven " in
      *" $sk "*)
        # Match on the cron NAME, which is prose ("Auto Improve"), while the skill
        # id is a slug ("auto-improve"). Measured 2026-11-04: a literal grep for
        # the slug returns 0 hits against a cron store that plainly contains the
        # job, i.e. a false "nothing will open it" on a healthy install. Compare
        # with hyphens folded to spaces so neither spelling has to be guessed.
        # `--all` is what includes DISABLED jobs, and this worker is disabled BY
        # DESIGN (gate-driven, never self-fires) — without it the one job we are
        # looking for is exactly the one hidden. There is no --include-disabled.
        # NO PIPE INTO `grep -q` HERE — this is a correctness requirement, not style.
        # The file runs under `set -o pipefail`; `grep -q` exits at the FIRST match
        # and SIGPIPEs whatever is still writing upstream, so the pipeline reports
        # 141 and a cron that is plainly present reads as ABSENT.
        # MEASURED 2026-11-04, and the result is counter-intuitive enough to write
        # down: `openclaw cron list | grep -q` did NOT reproduce it (10/10 exit 0 —
        # the CLI finishes writing into the pipe buffer before grep returns), while
        # `printf "$var" | grep -q` reproduced it 5/5 at exit 141. So the fragile
        # shape is the one that looks most harmless. Both are avoided below:
        # capture once, then match with pure bash — no second process, no pipe, no
        # exit code to misread.
        _sk_words="$(printf '%s' "$sk" | tr '-' ' ')"
        _cron_dump="$(openclaw cron list --all 2>/dev/null | tr '-' ' ')"
        # Case-fold both sides so cron prose ("Auto Improve") matches a slug
        # ("auto-improve") without depending on either one's capitalisation.
        _cron_lc="$(printf '%s' "$_cron_dump" | tr '[:upper:]' '[:lower:]')"
        _sk_lc="$(printf '%s' "$_sk_words" | tr '[:upper:]' '[:lower:]')"
        if ! command -v openclaw >/dev/null 2>&1; then
          _warn "skill $sk is cron-driven — openclaw CLI not on PATH, could not confirm its cron"
        elif case "$_cron_lc" in *"$_sk_lc"*) true ;; *) false ;; esac; then
          _pass "skill $sk reached via its cron (no AGENTS.md trigger expected)"
        else
          _warn "skill $sk is installed and cron-driven, but no matching cron was found — nothing will open it (re-run the installer)"
        fi
        continue
        ;;
    esac

    if grep -qF "\"$sk\"" "$WS/AGENTS.md" 2>/dev/null; then
      _pass "skill trigger present: $sk"
    else
      _orphans=$((_orphans+1))
      _warn "skill ORPHANED: $sk is installed but has no 'read skill \"$sk\"' trigger in AGENTS.md — it will never be opened (add a skills: line to the managed block)"
    fi
  done
  [ "$_orphans" -eq 0 ] && _pass "no orphaned skills (every installed dinomem skill has a trigger)"
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

# ── 8. 2026.9.x compat regressions (user report 2026-11-02) ─────────────────
# WHY THIS SECTION EXISTS: every bug in that report was SILENT. The install
# said OK; the damage showed up later as a cron that never ran, a graph that
# could not read its own index, or a gateway that refused to start. None of
# them were detectable by "is the file present", which is all this script used
# to ask. These checks assert the FIX IS ACTUALLY IN THE INSTALLED COPY, so a
# stale or half-upgraded workspace fails LOUD instead of reporting success.

# 8a. L3 cron chain. Two independent regressions, so two assertions.
#   * `&&` meant contradiction_check's exit 1 (it is a per-candidate guard; bare
#     invocation prints help and exits 1) killed the rest of the chain, so
#     confidence_engine NEVER ran and its log was never even created.
#   * `--report` is that script's only no-argument batch mode.
# Checked against the SYSTEM CRONTAB because that is the surface upsert_cron
# writes to (same rationale as section 2).
if [ "$LAYER" = "base+neuron" ]; then
  _l3line="$(crontab -l 2>/dev/null | grep -F 'dinomem-managed:memory_synthesis.py' | head -1)"
  if [ -z "$_l3line" ]; then
    _warn "L3 synthesis cron not in the system crontab — skipped chain checks"
  else
    case "$_l3line" in
      *"contradiction_check.py --report"*)
        _pass "L3 cron: contradiction_check called with --report" ;;
      *)
        _fail "L3 cron: contradiction_check called WITHOUT --report (prints help, exits 1 — chain dies)" ;;
    esac
    # The chain must not be && -joined. Look only at the part that chains the
    # three stages, so an unrelated && elsewhere in the line cannot false-fail.
    case "$_l3line" in
      *"memory_synthesis.log 2>&1 &&"*|*"contradiction_check.log 2>&1 &&"*)
        _fail "L3 cron: stages joined with && (one no-op stage silently skips confidence_engine)" ;;
      *)
        _pass "L3 cron: stages joined with ; (a no-op stage cannot skip the next)" ;;
    esac
  fi
fi

# 8b. Embedding decode. OpenClaw 2026.7.x stores chunk vectors as JSON TEXT;
# 2026.9.x stores a raw float32 BLOB. A bare json.loads() on the BLOB dies with
# UnicodeDecodeError (reported: byte 0xc5), which reads like a corrupt DB but is
# just the other valid format. The decoder lives in _schema_adapter.py so the
# "how do I read this index" rule has ONE home.
if [ "$LAYER" = "base+neuron" ]; then
  _adapter="$WS/procedures/_schema_adapter.py"
  _graph="$WS/procedures/memory_graph.py"
  if [ -f "$_adapter" ] && grep -q 'def decode_embedding' "$_adapter"; then
    _pass "_schema_adapter.py: decode_embedding present (handles BLOB + JSON)"
  else
    _fail "_schema_adapter.py: decode_embedding MISSING (2026.9.x BLOB vectors will crash memory_graph)"
  fi
  if [ -f "$_graph" ]; then
    if grep -q '_decode_embedding' "$_graph"; then
      _pass "memory_graph.py: decodes via _schema_adapter"
    else
      _fail "memory_graph.py: still decodes embeddings inline (UnicodeDecodeError on 2026.9.x)"
    fi
  fi
fi

# 8c. Embedding model cache. With git-lfs absent, a git clone of the HF repo
# leaves a ~134-byte TEXT POINTER where the weights should be. The file exists,
# so a presence-only check calls it "cached" — then docs_ingest dies much later
# with "header too large". Advisory (WARN): the cache is outside the workspace
# and a fresh box legitimately has no model yet.
_hf_root="${HF_HOME:-$HOME/.cache/huggingface}"
_e5_snaps="$_hf_root/hub/models--intfloat--multilingual-e5-small/snapshots"
if [ -d "$_e5_snaps" ]; then
  _e5_ok=0
  for _snap in "$_e5_snaps"/*/; do
    for _wf in model.safetensors pytorch_model.bin; do
      [ -f "$_snap/$_wf" ] || continue
      # stat follows symlinks on purpose: HF keeps real bytes in blobs/.
      _sz=$(stat -Lc %s "$_snap/$_wf" 2>/dev/null || stat -f %z "$_snap/$_wf" 2>/dev/null || echo 0)
      [ "${_sz:-0}" -ge 1048576 ] && _e5_ok=1
    done
  done
  [ "$_e5_ok" = 1 ] \
    && _pass "embedding model cache holds real weights (not a git-lfs pointer)" \
    || _warn "embedding model cache looks like a git-lfs POINTER (<1MB) — docs_ingest will fail 'header too large'; install git-lfs or let sentence-transformers re-download"
fi

# 8d. bundledDiscovery coherence. The key is VERSION-COUPLED: required on
# 2026.6/2026.7, unknown (and therefore config-invalidating) on 2026.9.x. The
# installers now probe the live schema instead of writing it blind. Here we only
# check the END STATE agrees with THIS OpenClaw, which is what actually matters
# to the user. Section 7 already catches a hard-invalid config; this names the
# specific cause so the fix is obvious rather than a schema-error hunt.
if command -v openclaw >/dev/null 2>&1; then
  _cfg="$(openclaw config file 2>/dev/null | tr -d '\r')"
  if [ -n "$_cfg" ] && [ -f "$_cfg" ] && grep -q '"bundledDiscovery"' "$_cfg" 2>/dev/null; then
    if openclaw config schema 2>/dev/null | grep -q '"bundledDiscovery"'; then
      _pass "plugins.bundledDiscovery set and known to this OpenClaw"
    else
      _fail "plugins.bundledDiscovery is set but this OpenClaw's schema does not know it (config invalid — re-run the installer to strip it)"
    fi
  fi
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
