---
# ── PEER REPRESENTATION SCHEMA (Honcho-port item #1) ─────────────────────────
# This file documents the rep format. It is NOT a real peer (leading underscore
# excludes it from routing). extract_user.py writes one file per known person at
#   memory/peers/<platform>_<platform_id>.md   e.g. peers/telegram_1083618205.md
#
# WHY composite key: bare sender_id is NOT globally unique. Telegram=numeric,
# WhatsApp=E.164 phone, Signal=UUID, iMessage=handle, Discord=snowflake. Two
# platforms can collide on the same id string. <platform>_<id> never collides.
#
# WHY this folder: memory/**/*.md is INDEXED (searchable via memory_search) but
# memory/*.md-only is AUTO-INJECTED. So peers/ reps are retrieved-not-injected
# for free — they never bloat always-on context, only surface on match.
# ─────────────────────────────────────────────────────────────────────────────
type: peer
platform: telegram          # telegram | whatsapp | signal | imessage | discord | ...
platform_id: "EXAMPLE"      # raw per-platform id (numeric / E.164 / uuid / handle)
person_ref:                 # (nullable) neuron Layer-2 person-link uuid. BASE leaves null.
display_name: ""            # from inbound metadata — NOT authoritative, hint only
first_seen: 2026-07-24
last_seen: 2026-07-24
interactions: 0             # meaningful-interaction count (drives derive-gating)
---

# Peer: <display_name or platform_id>

## FACTS
<!-- Person-DEFINING facts. Each: value (conf: high|med|low, DATE[, was: OLD DATE]).
     Contradiction => supersede-in-place with `was:` provenance, never append a 2nd line.
     Staleness => confidence DECAYS over unreconfirmed interactions, never auto-deleted. -->
- risk_appetite: <value> (conf: med, 2026-07-24)
- timeframe: <value> (conf: med, 2026-07-24)
- wants: <how they want to be helped> (conf: med, 2026-07-24)
- sophistication: <novice|intermediate|advanced> (conf: low, 2026-07-24)

## BEAT
<!-- Revealed behavior — what they ACTUALLY ask about (not what they claim). -->
- assets: []
- sectors: []
- recurring_topics: []

## RELATIONS
<!-- NEURON-tier. BASE leaves empty. Namespaced subject = filter key + privacy scope.
     Format: peer:<platform>:<id> -> verb -> object   e.g. peer:telegram:1083618205 -> trades -> ETH
     Fed into peers_graph.json (NEURON), NOT the global memory graph. Cross-peer
     aggregation ("who's in ETH") = OWNER-ONLY scoped query. -->

## LEDGER
<!-- This person's slice of the honest track record (their calls/asks + outcomes). -->

## PROVENANCE
<!-- Deriver audit trail: dated derive events, what changed, source archive. -->
- 2026-07-24 rep created (schema template)
