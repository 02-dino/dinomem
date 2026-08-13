#!/usr/bin/env python3
"""
route.py — Surface arbiter for self-modification intents (dinomem).

Single entry point BEFORE any leaf writer (config_tool/hook_tool/cron_tool/skill_tool).
The LLM classifies the request against the DECISION SCHEMA below and routes to ONE
surface. Prioritization is COST-ORDERED: root files load into context EVERY turn,
so they are the LAST resort. Prefer a trigger-gated surface (cron/hook/skill) whenever
the behavior is conditional (has a time trigger, an event trigger, or is only needed
sometimes). Fall to a root file ONLY when the behavior is always-on and has no trigger.

Usage (CLI):
  route.py classify        # print the machine-readable decision schema (JSON) for the LLM
  route.py surfaces        # list surfaces + their leaf tool
This tool does NOT write. It emits the schema; the LLM reasons over it, then calls the
selected leaf tool. Keeping it write-free avoids duplicating any leaf-tool logic.

## SURFACE COST ORDER (cheapest/most-preferred first; root files last)
  cron   : fires on a SCHEDULE only        -> zero always-on context cost
  hook   : fires on a GATEWAY EVENT only   -> zero always-on context cost
  skill  : loaded ON-DEMAND when relevant  -> ~1 line always-on (thin AGENTS.md trigger) + body only when read
  root   : injected EVERY turn             -> full always-on context cost -> the FALLBACK
           (only when the behavior has NO trigger). Among root files there is NO ranking:
           they are equal-weight homes for different content types (see discriminators 4-7).
           SOP/rule/when_to_use -> AGENTS.md is the CORRECT home, not a last resort.

## DISCRIMINATORS (ask in order; first hit wins; semantic, not keyword; multilingual)
  1. TIME TRIGGER   -> does it run on a clock/interval/date? ("every day", "in 2h",
                       "remind me", "check X periodically") ................ cron
  2. EVENT TRIGGER  -> does it react to a gateway lifecycle moment? ("every
                       time a session starts", "when a message comes in",
                       "on /reset", "before compaction", "at bootstrap") ... hook
  3. ON-DEMAND BODY -> is it procedural knowledge / a multi-step method only
                       needed SOMETIMES (not every turn)? A workflow, a
                       checklist, a how-to, domain steps the agent reads when
                       a specific task appears .......................... skill
  4. IDENTITY/STYLE -> is it WHO the agent is or HOW it sounds, always true,
                       no trigger? name/role/avatar -> IDENTITY.md;
                       tone/verbosity/personality -> SOUL.md ............. root(identity)
  5. USER FACT      -> a durable fact/preference ABOUT the human? name to
                       call them, timezone, context.
                       ⚠️ DO NOT write USER.md's MANAGED BLOCK directly — the
                       marker-bounded router block is COMPILED (compile_user.py
                       assembles owner block + user map from peer reps + owner
                       _pin_s each cycle; content OUTSIDE the markers is
                       hand-written and preserved). A write inside the markers is
                       CLOBBERED next cycle. Route a durable user fact to its
                       SURVIVING source instead:
                         - a persistent preference/rule the agent must obey ->
                           a memory `_pin_`/`_note_` file (extract_memory reads
                           these) OR AGENTS.md if it is a behavioral rule.
                         - a per-person fact -> the peer rep under memory/peers/
                           (that IS the durable source USER.md is derived from).
                       See AUTO-MANAGED ROOT FILES below. ......... memory/AGENTS.md (NOT USER.md)
  6. TOOL SPEC      -> an ON-DEMAND tool the AGENT ITSELF invokes at will
                       (path, inputs, capability)? .................. root(TOOLS.md)
                       PRINCIPLE: TOOLS.md documents ONLY on-demand tools the
                       LLM calls directly, and ONLY their SPEC + how-to-use.
                       EXCLUDE anything invoked purely by another script, a
                       cron, a hook, or an internal tool (operator/plumbing) —
                       those are NOT agent-facing and do not belong in TOOLS.md.
                       No operator runbooks, no cron/poller internals, no impl
                       detail — spec + invocation only.
  7. SOP / RULE     -> an SOP, behavioral rule/constraint, workflow, or
                       when_to_use with NO time/event trigger .......... root(AGENTS.md)

  NOTE: 4-7 are NOT a priority ladder — they are equal-weight content homes. Pick the file
  that matches the content type. AGENTS.md is the RIGHT home for SOPs/rules/when_to_use.

## ⛔ AUTO-MANAGED ROOT FILES — NEVER route a write here (they are OVERWRITTEN)
  In dinomem, two root files are AUTO-GENERATED and periodically OVERWRITTEN by
  the extraction crons — they are OUTPUTS, not editable homes:
    - MEMORY.md : regenerated by procedures/extract_memory.py from the memory/*.md
                  source files (_note_/_pin_/dated notes). Writing MEMORY.md directly
                  is pointless — it is a COMPILED VIEW; your edit vanishes on the next
                  extract cycle. To make something persist in MEMORY.md, write the
                  SOURCE note under memory/ and let extraction compile it in.
    - USER.md   : its managed router block is (re)compiled by procedures/compile_user.py
                  from memory/peers/ reps + owner _pin_s (extract_user.py writes the
                  peer reps; compile_user.py assembles them into USER.md). Same rule —
                  edit the SOURCE (peer rep or a _pin_), never the managed block. Content
                  OUTSIDE the markers is hand-written and preserved.
  ROUTER HARD RULE: classify NEVER selects MEMORY.md, nor USER.md's MANAGED BLOCK,
  as a write target. If a request's natural home *looks* like one of them, redirect
  to its durable source (a memory/ note, a peer rep, or AGENTS.md for a rule). These
  targets are absent from every write path below BY DESIGN. (Scope note: MEMORY.md is
  off-limits ENTIRELY — wholesale-regenerated. For USER.md only the marker-bounded
  router block is off-limits; content OUTSIDE the markers is a legitimate hand-edit
  surface — but auto-routed FACTS still belong in their source, never typed in by hand.)

## 🔒 DON'T-TRUST-THIS-SESSION — durability MUST live on a surface the NEXT session loads
  THE PRINCIPLE (the reason this router exists): a fix/behavior that lives only in
  your LIVE CONTEXT — your working memory this turn, "I'll remember to do X",
  something you did but did not WRITE to a loaded surface — is GONE next session.
  A fresh session does NOT inherit this session's context. So durability comes ONLY
  from placing the behavior on a surface the next session actually loads:
    - root file (IDENTITY/SOUL/AGENTS/TOOLS.md) — injected EVERY turn, always present.
    - hook — fires on its gateway event, every session, no context needed.
    - skill — thin always-on trigger + on-demand body, loaded when its task recurs.
    - cron — fires on its schedule regardless of any session.
  If the ONLY place a behavior 'lives' is this conversation, it is NOT durable — it
  is a promise that dies at compaction. ROUTING RULE: whenever a request is "make the
  agent reliably do/remember X", you MUST land X on one of the four inherited surfaces
  above (never 'just keep it in mind', never a note you don't verify on disk), and
  VERIFY it landed. Do not trust THIS session to carry it forward — trust the surface.

  COROLLARY (auto-managed files): even among loaded surfaces, MEMORY.md and USER.md's
  MANAGED BLOCK do NOT count as durable homes — they are regenerated/overwritten by the
  extraction crons (see ⛔ AUTO-MANAGED ROOT FILES). Note the scope differs: MEMORY.md is
  rewritten WHOLESALE by extract_memory.py (no safe zone), whereas only USER.md's
  marker-bounded router block is recompiled by compile_user.py — USER.md content OUTSIDE
  the markers is hand-written and durable. Durable = an inherited surface that is NOT
  auto-regenerated: root files you hand-maintain (AGENTS/SOUL/IDENTITY/TOOLS.md), a
  hook, a skill, a cron, or a memory/ _note_/_pin_ SOURCE the crons read FROM.

## CRON SUB-ROUTE (once surface=cron): linux crontab vs openclaw cron
  Two cron BACKENDS exist; pick by whether the job needs the gateway/agent:
    - LINUX CRONTAB  -> pure-script infra that MUST run even if the OpenClaw
      gateway is down (memory extraction, backups, session reset, cleanup).
      Zero gateway context cost; survives gateway restarts/outages. This is
      how dinomem's OWN install-time infra crons are registered (install.sh).
    - OPENCLAW CRON  -> anything that needs agent context, model routing,
      delivery, or an agentTurn/message payload (T2/T3), OR a gated worker.
      Managed by the gateway; this is what tools/cron_tool.py writes.
  RULE: deterministic script that must survive gateway-down -> linux crontab.
        needs agent/model/delivery (T1 gate worker, T2/T3 message) -> openclaw cron.
  Default for USER-requested scheduling -> openclaw cron (via cron_tool.py). Only
  base-infra plumbing chooses linux crontab, and that choice is made at install.

## CRON CONTEXT-WEIGHT (once surface=cron AND payload=agentTurn/message): light vs full
  The SAME logic that makes root files the expensive default for surface routing applies
  PER-FIRE to a cron's context: the bootstrap root files (AGENTS/SOUL/IDENTITY/USER/TOOLS)
  are injected into the run on EVERY fire. A mechanical cron re-pays that token cost each
  time for context it never reads. So classify the job's context need, not just its tier:
    - needs_persona_or_root_rules -> FULL context (default; omit --light-context).
      The job's quality depends on SOUL/IDENTITY tone, a TOOLS spec, or an AGENTS.md
      behavioral rule it does NOT restate in its own prompt. Public-facing / voice /
      judgment jobs usually live here.
    - self_contained_mechanical   -> --light-context (skip bootstrap injection).
      The prompt carries everything the job needs (script-run + fill-fields + format /
      report). The root files add nothing but tokens on every fire.
  cron_tool.py exposes this as --light-context (AXIS 5, message jobs only). It is a NO-OP
  for command/system-event jobs (they inject no root context). DEFAULT light for a purely
  mechanical agentTurn; keep FULL when persona/root-rule dependence is real. When unsure,
  prefer FULL (correctness over a few tokens) — light is safe once the prompt is self-contained.
  This axis is ORTHOGONAL to model-tier: a job can be cheap-model AND full-context, or
  default-model AND light-context; decide context need independently of which model runs it.

## TRIGGER RE-CHECK (only about surface, not about avoiding any root file)
  The single hierarchy is trigger-gated (cron/hook/skill) vs always-on (root). Before routing
  to ANY root file, re-test whether the behavior actually has a trigger that fits it better:
    - reacts to a gateway event? (e.g. "always X on inbound" -> message:received hook) -> hook
    - needed only for a specific TASK class? -> skill (thin trigger + on-demand body)
    - runs on a schedule? -> cron
  If none fit, it's genuinely always-on -> route to the matching root file by content type.

## SKILL SPECIAL CASE (trigger vs body split)
  A skill is NOT fully root-free: it needs a SHORT trigger so the agent knows WHEN to read it.
  - trigger  -> ONE line in AGENTS.md when_to_use (or the skill `description` frontmatter) -> minimal always-on cost
  - body     -> the SKILL.md itself -> loaded on-demand only -> zero cost until read
  Keep the trigger to a single line; never inline the skill body into a root file.

## OUTPUT CONTRACT (what the LLM does with this)
  Pick exactly ONE surface. If two discriminators fire (e.g. a scheduled task that also
  needs procedural steps), the TRIGGER wins for placement (cron/hook), and the steps go in
  its payload/handler or a skill it calls — never duplicated into a root file.
  Then call the mapped leaf tool. If genuinely ambiguous, ask ONE question, then route.
"""
import argparse
import json

SURFACES = {
    "cron":  {"leaf": "tools/cron_tool.py",  "skill": "cron-config",  "cost": "none (schedule-gated)"},
    "hook":  {"leaf": "tools/hook_tool.py",  "skill": "hook-config",  "cost": "none (event-gated)"},
    "skill": {"leaf": "tools/skill_tool.py", "skill": "skill-config", "cost": "~1 line trigger + on-demand body"},
    "root":  {"leaf": "tools/config_tool.py","skill": "self-config",   "cost": "full always-on (every turn)"},
}

# Machine-readable decision tree. Ordered list = evaluation order; first match wins.
SCHEMA = {
    "cost_order": ["cron", "hook", "skill", "root"],
    "root_intra_order": ["IDENTITY.md", "SOUL.md", "TOOLS.md", "AGENTS.md"],
    # HARD FORBIDDEN write targets: auto-generated root files. classify() must NEVER
    # emit either as a `file`. They are COMPILED OUTPUTS overwritten by the extraction
    # crons (extract_memory.py -> MEMORY.md, extract_user.py -> USER.md). To persist,
    # write the SOURCE the cron reads (a memory/ _note_/_pin_, or a peer rep), not these.
    "forbidden_write_targets": {
        "MEMORY.md": "auto-generated by extract_memory.py from memory/*.md; edits clobbered next cycle. Write the source note under memory/ instead.",
        "USER.md": "managed router block compiled by compile_user.py from memory/peers/ reps + owner _pin_s (extract_user.py writes the reps); edits INSIDE the markers clobbered next cycle. Write a memory/ _pin_ or memory/peers/ rep instead. Content outside the markers is hand-written and preserved.",
    },
    "discriminators": [
        {"id": 1, "test": "runs_on_clock_interval_or_date", "surface": "cron",  "leaf": "tools/cron_tool.py"},
        {"id": 2, "test": "reacts_to_gateway_event",        "surface": "hook",  "leaf": "tools/hook_tool.py"},
        {"id": 3, "test": "procedural_knowledge_needed_sometimes", "surface": "skill", "leaf": "tools/skill_tool.py"},
        {"id": 4, "test": "identity_or_style_always_true",  "surface": "root", "file": ["IDENTITY.md", "SOUL.md"], "leaf": "tools/config_tool.py"},
        {"id": 5, "test": "durable_user_fact_or_pref",      "surface": "root", "file": ["AGENTS.md"], "leaf": "tools/config_tool.py",
         "redirect": "USER.md's MANAGED ROUTER BLOCK is FORBIDDEN as a write target: it is compiled by compile_user.py (owner block + user map from peer reps + owner _pin_s) and OVERWRITTEN each cycle — you NEVER hand-edit inside the markers (content outside them IS hand-editable). Split the intent by TYPE, do not funnel both to one file: (a) a BEHAVIORAL rule/preference the agent must OBEY ('always call me X', 'never hard-sell') -> AGENTS.md (a loaded, hand-maintained rule surface). (b) a BIOGRAPHICAL fact (name, timezone, context) -> write the SOURCE compile_user reads (a memory/ _pin_, or a memory/peers/ rep); USER.md's router block then gets it COMPILED IN automatically on the next cycle. Either way you change USER.md's *content* by editing its SOURCE, never the managed block itself. AGENTS.md does NOT replace USER.md — it is only the home for case (a)."},
        {"id": 6, "test": "on_demand_tool_agent_invokes_directly", "surface": "root", "file": ["TOOLS.md"], "leaf": "tools/config_tool.py",
         "principle": "TOOLS.md = ON-DEMAND agent-facing tools ONLY; spec + how-to-use ONLY. EXCLUDE script/cron/hook/internal-tool-only invocations (operator plumbing) and any non-spec impl detail."},
        {"id": 7, "test": "sop_or_rule_or_when_to_use_no_trigger", "surface": "root", "file": ["AGENTS.md"], "leaf": "tools/config_tool.py"},
    ],
    "cron_backend_subroute": {
        "linux_crontab": "pure-script infra that must run even if gateway is down (extract/backup/reset/cleanup); zero gateway cost; install-time only",
        "openclaw_cron": "needs agent context / model routing / delivery / agentTurn payload (T1 gate, T2 cheap, T3 default); written by tools/cron_tool.py",
        "rule": "survives-gateway-down deterministic -> linux crontab; needs-agent-or-model-or-delivery -> openclaw cron",
        "default_for_user_requests": "openclaw cron",
    },
    "cron_context_weight": {
        "applies_to": "openclaw cron, agentTurn/message payload only (no-op for command/system-event)",
        "axis": "orthogonal to model-tier: context-need is decided independently of which model runs the job",
        "full": "needs_persona_or_root_rules -> omit --light-context (DEFAULT): job quality depends on SOUL/IDENTITY tone, a TOOLS spec, or an AGENTS.md rule it does not restate in its own prompt",
        "light": "self_contained_mechanical -> --light-context: prompt carries everything (script-run + fill-fields + format/report); bootstrap root files add only tokens on every fire",
        "why": "bootstrap root files (AGENTS/SOUL/IDENTITY/USER/TOOLS) inject on EVERY fire; a mechanical cron re-pays that for context it never reads",
        "default": "light for a purely mechanical agentTurn; full when persona/root-rule dependence is real; when unsure prefer full (correctness over a few tokens)",
        "flag": "cron_tool.py --light-context (AXIS 5)",
    },
    "trigger_recheck_before_root": [
        "could_it_be_a_hook_on_its_event -> prefer hook",
        "needed_only_for_task_class -> prefer skill",
        "runs_on_a_schedule -> prefer cron",
    ],
    "skill_split": {"trigger": "one_line AGENTS.md when_to_use OR skill description", "body": "SKILL.md on-demand"},
    "tie_break": "trigger_wins_for_placement; steps go in payload/handler/skill; never duplicate into root",

    # ── UPGRADE 1: TOTAL-COST MODEL (frequency-aware) ─────────────────────────
    # The naive cost_order above ranks by ALWAYS-ON weight only, which treats every
    # cron as free. FALSE for a high-frequency FULL-CONTEXT cron: it re-pays the
    # whole bootstrap-root token load on EVERY fire. A */5 full-context agentTurn
    # fires ~288x/day; that can EXCEED a one-line AGENTS.md rule's per-turn cost.
    # So the cheapest surface can FLIP with frequency. Fold fire-rate in:
    #
    #   total_daily_context_cost =
    #       always_on_weight_per_turn * turns_per_day          # root files pay this
    #     + per_fire_context_weight  * fires_per_day           # crons/hooks pay this
    #
    # where per_fire_context_weight = FULL bootstrap load if the cron is full-context
    # (agentTurn without --light-context), ~0 if light-context / command / event.
    # Hooks fire on events (usually low-rate, light) => ~0. Skills pay ~1 line
    # always-on + body only when read => still cheapest for recurring task-class work.
    "total_cost_model": {
        "why": "always-on ordering treats crons as free; a high-frequency FULL-CONTEXT cron re-pays bootstrap tokens every fire and can cost MORE than a root rule. Rank by TOTAL daily context tokens, not just always-on.",
        "formula": "total_daily = always_on_per_turn*turns_per_day + per_fire_context*fires_per_day",
        "inputs": {
            "always_on_per_turn": "root file: FULL bootstrap share every turn; skill: ~1 line; cron/hook: 0",
            "per_fire_context": "full-context agentTurn cron: FULL bootstrap load; light-context/command/system-event cron: ~0; hook: ~0",
            "fires_per_day": "derive from the schedule: */5=288, */15=96, hourly=24, daily=1, event-driven=estimate event rate",
            "turns_per_day": "conversation turns the agent takes/day (root files are paid on each)",
        },
        "flip_rule": "if a cron is FULL-CONTEXT and HIGH-FREQUENCY (fires_per_day large), its total can exceed a one-line AGENTS.md rule. Then: (a) FIRST try to make it light-context (self-contained prompt) -> per_fire_context ~0 -> cron wins again; (b) only if it genuinely needs root persona/rules every fire AND fires rarely, keep full cron; (c) if it is really an always-on behavior mis-modeled as a cron, a root rule may be cheaper -> reconsider surface.",
        "primary_still": "context-cost remains the PRIMARY axis; this only refines it with frequency. cost_order stays the default when per_fire_context can be driven to ~0 (which light-context usually achieves).",
        "g1_safe": "pure token-accounting; no market/TA content.",
    },

    # ── UPGRADE 2: MULTI-AXIS ARBITRATION (tie-breakers beyond context) ───────
    # When two surfaces are close on total context cost, break the tie on the
    # OTHER real costs. These are SECONDARY (never override a clear context win),
    # applied only on near-ties or as explicit overrides for safety.
    "secondary_axes": {
        "order": ["total_context_cost(primary)", "failure_blast_radius", "maintenance_staleness", "discoverability"],
        "failure_blast_radius": "how bad if this surface's content is wrong/breaks? A malformed root file degrades EVERY turn (high blast radius); a broken cron lane is isolated (gate swallows it). Prefer the surface whose failure is CONTAINED when cost is a tie. HARD OVERRIDE: never place something on a surface where a formatting error bricks bootstrap if an isolated surface fits equally.",
        "maintenance_staleness": "will this content go stale and need edits? Trigger-gated surfaces (cron/hook/skill) are edited in one place; a rule duplicated across root files rots. Prefer single-home surfaces for churny content.",
        "discoverability": "will a FUTURE session find/apply it when relevant? A skill's thin trigger + a hook's event binding are self-surfacing; a buried root paragraph may be ignored. Prefer self-surfacing placement for conditional behaviors.",
        "rule": "these NEVER beat a clear total-context winner; they decide near-ties and can HARD-OVERRIDE only for failure_blast_radius (safety).",
    },

    # ── UPGRADE 3: SAFETY FLOOR (shared with the cron-gate write-path) ────────
    # route.py is write-free, but the LEAF tools it selects (config_tool/cron_tool/
    # hook_tool/skill_tool) DO write. The forbidden-target rule above is today a
    # PLEA to the LLM. It must be ENFORCED at the leaf, via the SAME version-matched
    # validated-write floor the cron-gate lib uses. The shared primitive is NOT a
    # bash file -- it's the local `openclaw config` validator itself (version-matched
    # by construction, offline-proof). Both the bash gate (gate_lib safe_config_write)
    # and the Python leaf tools are thin adapters over that same CLI.
    "safety_floor": {
        "principle": "version-matched local validator is the unbypassable floor; docs are version-pinned enrichment, never authority.",
        "enforce_at": "leaf writers (config_tool.py etc.), NOT here -- route.py stays write-free.",
        "rules": [
            "config writes go through `openclaw config patch/set` (validated, refuses invalid) then `openclaw config validate`; NEVER raw openclaw.json edits.",
            "forbidden_write_targets (MEMORY.md, USER.md managed block) are BLOCKED programmatically at the leaf, not just documented -- a write attempt to them errors and redirects to the source.",
            "schema_field_ok: confirm a config field exists via local `openclaw config.schema.lookup` (version-matched) before writing it.",
        ],
        "offline": "the validator ships in the installed binary => works offline, matches the exact installed version. 'latest' docs are never the authority.",
    },

    # ── UPGRADE 4: VERIFICATION (test-don't-assume, mechanized) ───────────────
    # The docstring says 'VERIFY it landed' but ships no checker. `route.py verify`
    # confirms a routed write actually reached its surface, turning the plea into a
    # mechanized post-condition.
    "verify_contract": {
        "why": "don't-trust-this-session: a behavior is durable ONLY if it landed on an inherited surface. Verify on disk, don't assume.",
        "how": {
            "cron": "`openclaw cron list --all` shows the new job id / name.",
            "hook": "the hook dir exists under $WS/hooks/<name>/ and `openclaw hooks list` shows it enabled.",
            "skill": "SKILL.md exists under $WS/skills/<name>/ AND its one-line trigger is present in AGENTS.md.",
            "root": "grep the intended content in the target root file (AGENTS/SOUL/IDENTITY/TOOLS.md); confirm it is NOT in a forbidden target.",
        },
        "cli": "route.py verify <surface> <needle> [--file F] -> exit 0 if present, 1 if missing.",
    },
}

def _verify(surface, needle, target_file=None):
    """Mechanized 'did the routed write actually land' check (test-don't-assume).
    Returns (ok: bool, detail: str). No writes; read-only probes. Fail-closed:
    an unknown surface or a probe error returns ok=False (never a false PASS).
    """
    import os, subprocess
    ws = os.environ.get("OPENCLAW_WORKSPACE", os.path.expanduser("~/.openclaw/workspace"))
    surface = (surface or "").strip().lower()

    def _run(cmd):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            return (r.returncode, (r.stdout or "") + (r.stderr or ""))
        except Exception as e:
            return (127, str(e))

    if surface == "cron":
        rc, out = _run(["openclaw", "cron", "list", "--all"])
        if rc == 127:
            return False, "openclaw CLI unavailable — cannot verify cron (fail-closed)"
        return (needle in out), ("found in cron list" if needle in out else "NOT in cron list")

    if surface == "hook":
        hook_dir = os.path.join(ws, "hooks", needle)
        exists = os.path.isdir(hook_dir)
        rc, out = _run(["openclaw", "hooks", "list"])
        listed = (needle in out) if rc != 127 else exists
        return (exists and listed), (f"hook dir={exists} listed={listed}")

    if surface == "skill":
        sk = os.path.join(ws, "skills", needle, "SKILL.md")
        exists = os.path.isfile(sk)
        agents = os.path.join(ws, "AGENTS.md")
        trig = False
        try:
            with open(agents, encoding="utf-8") as fh:
                trig = needle in fh.read()
        except Exception:
            trig = False
        return (exists and trig), (f"SKILL.md={exists} trigger_in_AGENTS={trig}")

    if surface == "root":
        # forbidden-target guard: verifying a write INTO a forbidden target is itself a failure.
        base = os.path.basename(target_file or "")
        if base in SCHEMA["forbidden_write_targets"]:
            return False, f"{base} is a FORBIDDEN write target — content must live in its source, not here"
        if not target_file:
            return False, "root verify needs --file <AGENTS|SOUL|IDENTITY|TOOLS.md>"
        path = target_file if os.path.isabs(target_file) else os.path.join(ws, target_file)
        try:
            with open(path, encoding="utf-8") as fh:
                present = needle in fh.read()
            return present, (f"found in {base}" if present else f"NOT in {base}")
        except Exception as e:
            return False, f"cannot read {base}: {e}"

    return False, f"unknown surface '{surface}' (expected cron|hook|skill|root)"


def _dup_scan(path, min_run=5):
    """Advisory copy-paste detector for a just-built script (build-quality DRY
    floor). Flags a block of >= min_run CONSECUTIVE non-trivial lines that appears
    2+ times -- the exact anti-pattern that shipped wrong this session (a 16-line
    gate block pasted across two files). HEURISTIC not proof: misses semantic dup,
    may flag legit repetition, so it is ADVISORY (a hint, not a verdict). Returns
    (clean, detail). Read-only; fail-closed (error -> clean=False so a broken scan
    never falsely reports 'no dup').
    """
    import os
    if not path or not os.path.isfile(path):
        return False, "cannot read %r (dup-scan needs a real file)" % (path,)
    try:
        with open(path, encoding="utf-8") as fh:
            raw = fh.readlines()
    except Exception as e:
        return False, "cannot read %s: %s" % (os.path.basename(path), e)
    trivial = {"{", "}", "fi", "done", "esac", "end", "else", ")", "do", "then"}
    norm = []
    for ln in raw:
        s = ln.strip()
        if not s or s.startswith("#") or s.startswith("//") or s in trivial:
            norm.append(None)
        else:
            norm.append(s)
    n = len(norm)
    seen = {}
    dup_at = None
    for i in range(n - min_run + 1):
        window = norm[i:i + min_run]
        if any(w is None for w in window):
            continue
        key = "\n".join(window)
        if key in seen and i - seen[key] >= min_run:
            dup_at = (seen[key], i)
            break
        seen.setdefault(key, i)
    if dup_at is None:
        return True, "no duplicated block of >=%d lines found" % min_run
    a, b = dup_at
    length = min_run
    while (a + length < b and b + length < n
           and norm[a + length] is not None and norm[a + length] == norm[b + length]):
        length += 1
    return False, ("ADVISORY: lines %d-%d repeat at %d-%d (%d identical lines) -- "
                   "factor into ONE shared function and call it (build-quality DRY "
                   "floor). Heuristic; confirm it's real before refactoring."
                   % (a+1, a+length, b+1, b+length, length))

def main():
    p = argparse.ArgumentParser(description="Surface arbiter for dinomem self-modification intents")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("classify")
    sub.add_parser("surfaces")
    v = sub.add_parser("verify", help="confirm a routed write actually landed on its surface (exit 0=present, 1=missing)")
    v.add_argument("surface", help="cron|hook|skill|root")
    v.add_argument("needle", help="job name/id, hook/skill dir name, or content substring")
    v.add_argument("--file", dest="file", default=None, help="root target file (AGENTS.md/SOUL.md/IDENTITY.md/TOOLS.md)")
    d = sub.add_parser("dup", help="advisory copy-paste scan of a just-built script (build-quality DRY floor; exit 0=clean, 1=dup)")
    d.add_argument("file", help="path to the script/file you just built")
    d.add_argument("--min-run", dest="min_run", type=int, default=5, help="min consecutive repeated lines to flag (default 5)")
    args = p.parse_args()
    if args.cmd == "surfaces":
        print(json.dumps(SURFACES, indent=2))
    elif args.cmd == "verify":
        ok, detail = _verify(args.surface, args.needle, args.file)
        print(json.dumps({"surface": args.surface, "needle": args.needle, "ok": ok, "detail": detail}, indent=2))
        raise SystemExit(0 if ok else 1)
    elif args.cmd == "dup":
        clean, detail = _dup_scan(args.file, args.min_run)
        print(json.dumps({"file": args.file, "clean": clean, "detail": detail}, indent=2))
        raise SystemExit(0 if clean else 1)
    else:
        print(json.dumps(SCHEMA, indent=2))

if __name__ == "__main__":
    main()
