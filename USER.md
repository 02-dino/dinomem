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
-->

## OWNER BLOCK
- Owner: Dino (@dinotlgrm, telegram:1083618205). Full access.
- Core operating prefs (always relevant regardless of who is speaking):
  - Evidence over consensus; no hedged neutrality — give directional calls.
  - Always append NFA/DYOR disclaimer. No technical-analysis indicators ever.
  - Keep an honest track-record ledger (record misses too).
- Owner rep detail: peers/telegram_1083618205.md

## PEER MAP
<!-- schema: - <platform>:<id> — <name/handle>, [tags], one-liner -> peers/<platform>_<id>.md -->
- telegram:1083618205 — Dino (@dinotlgrm), [owner, market-analysis], bot owner -> peers/telegram_1083618205.md
