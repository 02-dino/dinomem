# USER.md — Peer Router

<!--
  This is a ROUTER, not a facts-store. Always-on, bounded, MEMORY.md-for-people.
  Two parts: OWNER BLOCK (always relevant) + PEER MAP (1 line/known active person).
  Full per-person reps live in memory/peers/<platform>_<platform_id>.md
  (indexed by memory_search, NOT auto-injected — retrieved on demand).
  Routing = (platform, sender_id) from inbound meta -> direct file lookup at
  peers/<platform>_<sender_id>.md. Map presence is NOT required to route; the map
  is a human-readable index capped to owner + ACTIVE peers (~50-100 lines) so
  always-on size stays DECOUPLED from N users.
  PRIVACY: a peer rep may enter context ONLY when the active-speaker
  (platform, id) matches that rep, OR the owner explicitly asks about that person.

  FILL THIS IN at install: replace the OWNER BLOCK placeholders with your own
  identity + operating preferences. Leave the PEER MAP empty — extract_user.py
  populates it as people interact.
-->

## OWNER BLOCK
- Owner: <YOUR_NAME> (<@your_handle>, <platform>:<your_sender_id>). Full access.
- Core operating prefs (always relevant regardless of who is speaking):
  - <e.g. tone, domain focus, hard rules the agent must always follow>
  - <e.g. output format preferences>
  - <e.g. disclaimers or constraints to always apply>
- Owner rep detail: peers/<platform>_<your_sender_id>.md

## PEER MAP
<!-- schema: - <platform>:<id> — <name/handle>, [tags], one-liner -> peers/<platform>_<id>.md -->
<!-- (empty on a fresh install — extract_user.py adds one line per known active person) -->
