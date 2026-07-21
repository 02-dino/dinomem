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
                       call them, timezone, context ................. root(USER.md)
  6. TOOL SPEC      -> a callable tool/script the agent invokes (path,
                       inputs, capability)? .......................... root(TOOLS.md)
  7. SOP / RULE     -> an SOP, behavioral rule/constraint, workflow, or
                       when_to_use with NO time/event trigger .......... root(AGENTS.md)

  NOTE: 4-7 are NOT a priority ladder — they are equal-weight content homes. Pick the file
  that matches the content type. AGENTS.md is the RIGHT home for SOPs/rules/when_to_use.

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
    "root_intra_order": ["IDENTITY.md", "SOUL.md", "USER.md", "TOOLS.md", "AGENTS.md"],
    "discriminators": [
        {"id": 1, "test": "runs_on_clock_interval_or_date", "surface": "cron",  "leaf": "tools/cron_tool.py"},
        {"id": 2, "test": "reacts_to_gateway_event",        "surface": "hook",  "leaf": "tools/hook_tool.py"},
        {"id": 3, "test": "procedural_knowledge_needed_sometimes", "surface": "skill", "leaf": "tools/skill_tool.py"},
        {"id": 4, "test": "identity_or_style_always_true",  "surface": "root", "file": ["IDENTITY.md", "SOUL.md"], "leaf": "tools/config_tool.py"},
        {"id": 5, "test": "durable_user_fact_or_pref",      "surface": "root", "file": ["USER.md"], "leaf": "tools/config_tool.py"},
        {"id": 6, "test": "callable_tool_spec",             "surface": "root", "file": ["TOOLS.md"], "leaf": "tools/config_tool.py"},
        {"id": 7, "test": "sop_or_rule_or_when_to_use_no_trigger", "surface": "root", "file": ["AGENTS.md"], "leaf": "tools/config_tool.py"},
    ],
    "trigger_recheck_before_root": [
        "could_it_be_a_hook_on_its_event -> prefer hook",
        "needed_only_for_task_class -> prefer skill",
        "runs_on_a_schedule -> prefer cron",
    ],
    "skill_split": {"trigger": "one_line AGENTS.md when_to_use OR skill description", "body": "SKILL.md on-demand"},
    "tie_break": "trigger_wins_for_placement; steps go in payload/handler/skill; never duplicate into root",
}

def main():
    p = argparse.ArgumentParser(description="Surface arbiter for dinomem self-modification intents")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("classify")
    sub.add_parser("surfaces")
    args = p.parse_args()
    if args.cmd == "surfaces":
        print(json.dumps(SURFACES, indent=2))
    else:
        print(json.dumps(SCHEMA, indent=2))

if __name__ == "__main__":
    main()
