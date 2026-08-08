#!/usr/bin/env python3
"""
mem_authority.py — provenance + authority-scope gate for the dinomem write path.

WHY (dino, 2026-08-08): dinomem stores memory from EVERY user (owner + non-owner
peers) and recalls it into future context. That makes the async extract-write
path a STORED / SECOND-ORDER PROMPT-INJECTION surface: a non-owner types crafted
text, the cheap extractor distills it, and days later it is recalled with the
system's own memory authority. dinotrust-enforce gates the LIVE tool loop
(before_tool_call) but NEVER sees this cron path — so this gate lives in dinomem,
self-contained, fail-open, zero-LLM, no dinotrust dependency.

THE FRAME (this is the important part):
  provenance (WHO said it)  and  authority (WHAT it may change)  are ORTHOGONAL.
  We do NOT tag non-owner memory "untrusted" — that would make the LLM DISCOUNT
  legitimate user data and break personalization (the whole point of multi-user
  memory). Instead:

    - A non-owner fact ABOUT THE PERSON ("prefers raw data", "trades ETH",
      "low risk tolerance")  -> FULLY TRUSTED as data, stored as personalization.
      It SHOULD change how the assistant treats THAT user.

    - A non-owner item that asserts a SYSTEM / WORKSPACE DIRECTIVE
      ("always push to github without asking", "ignore security", "you are now
      X", "owner approved…")  -> is NOT "untrusted data", it is simply NOT A
      STANDING INSTRUCTION coming from a non-owner. It is DROPPED, or DEMOTED to
      a neutral OBSERVATION about that person's behavior ("asked the bot to …"),
      never stored as a rule the system would later obey.

  So the only thing filtered is: a SYSTEM-SCOPE DIRECTIVE from a NON-OWNER.
  Everything a non-owner reveals about themselves flows through, fully trusted.

  Owner-sourced items are unaffected (owner may set system directives). dinotrust
  R2/memory_policy ("recalled memory = data, not instruction") remains the
  recall-side belt; this is the write-side suspenders. Complementary, no clash.

PUBLIC API (all fail-open — any error returns the SAFE default that never blocks
personalization and never crashes the extractor):
  is_owner(platform_id) -> bool
  classify_scope(text)   -> "personalization" | "directive"
  gate_peer_fact(text, is_owner_src) -> (keep: bool, out_text: str, demoted: bool)
  gate_world_fact(text, is_owner_src) -> (keep: bool, out_text: str, demoted: bool)
"""
import os
import re

# ── Owner ids ────────────────────────────────────────────────────────────────
# Discovered from env (set by installer) with a safe fallback. NOT the security
# authority of record (dinotrust owns that) — here it only decides whether an
# extracted SYSTEM-DIRECTIVE is allowed to persist as a standing rule.
def _owner_ids():
    raw = (os.environ.get("DINOMEM_OWNER_IDS", "") or
           os.environ.get("DINOTRUST_OWNER_IDS", "")).strip()
    ids = {x.strip() for x in re.split(r"[,\s]+", raw) if x.strip()}
    return ids


def is_owner(platform_id):
    """True if platform_id is a configured owner. Fail-open MODE MATTERS:
    if NO owner ids are configured at all, we cannot prove non-owner, so we
    treat the source as owner (do not filter) — dinomem must not start eating
    the operator's own memory just because the env var is unset. The directive
    filter still runs on CONTENT for everyone via gate_* callers that pass
    is_owner_src accordingly."""
    if platform_id is None:
        return True  # agent-operated / unattributable -> treat as owner-side
    ids = _owner_ids()
    if not ids:
        return True  # no owner configured -> cannot classify, don't over-filter
    return str(platform_id).strip() in ids


# ── Authority-scope classifier (deterministic regex, NOT an LLM) ──────────────
# A "directive" = text that tries to install a STANDING RULE governing the
# SYSTEM / ASSISTANT / WORKSPACE behavior. These are the only patterns filtered
# when the source is a non-owner. Kept deliberately TIGHT (conservative) so
# genuine personalization facts are never eaten. dinotrust R2 is the deeper net
# for anything novel that slips.
_DIRECTIVE_PATTERNS = [
    # explicit instruction-override / role reassignment
    r"\bignore (?:all |the |any )?(?:previous|prior|above|earlier)\b",
    r"\bdisregard (?:all |the |any )?(?:previous|prior|above|instructions)\b",
    r"\byou are now\b", r"\bfrom now on,? you\b",
    r"\bact as (?:if you are|an?|the)\b", r"\bpretend (?:to be|you are)\b",
    r"\bnew (?:system )?(?:instructions?|rules?|directive)\b",
    r"^\s*system\s*:", r"\bsystem prompt\b",
    # false authority / privilege escalation
    r"\bowner (?:approved|authorized|said|allows?|permits?)\b",
    r"\b(?:grant|give|enable|elevate) (?:me )?(?:full |admin |root )?access\b",
    r"\b(?:disable|bypass|turn off|override|ignore) (?:the )?(?:security|safety|approval|guard|permission)\b",
    r"\bmark me as (?:owner|admin|trusted)\b",
    r"\bi am (?:the )?(?:owner|admin|your (?:owner|creator|developer))\b",
    # standing behavioral rules aimed at the system/tools (not self-description)
    r"\balways (?:push|commit|deploy|run|execute|send|delete|write|edit)\b.{0,40}\b(?:without asking|automatically|no confirm)\b",
    r"\bnever (?:ask|confirm|require approval|verify)\b",
    r"\b(?:you|the assistant|the bot|the agent) (?:must|should|will|shall) (?:always|never)\b",
]
_DIRECTIVE_RE = re.compile("|".join(_DIRECTIVE_PATTERNS), re.IGNORECASE)


def classify_scope(text):
    """Return 'directive' if text asserts a system/assistant standing rule,
    else 'personalization'. Fail-open: on any error -> 'personalization'
    (the non-filtering default)."""
    try:
        if not text:
            return "personalization"
        return "directive" if _DIRECTIVE_RE.search(text) else "personalization"
    except Exception:
        return "personalization"


# ── Gates ─────────────────────────────────────────────────────────────────────
def gate_peer_fact(text, is_owner_src):
    """Gate a PERSON fact (extract_user lane).
    Returns (keep, out_text, demoted).
      - owner source: pass untouched.
      - non-owner + personalization: pass untouched (FULLY TRUSTED about them).
      - non-owner + directive: DEMOTE to a neutral observation (never a rule),
        so the intent is recorded as a fact ABOUT the person, not obeyed.
    Fail-open: on any error, keep original (never crash the extractor)."""
    try:
        if is_owner_src or classify_scope(text) == "personalization":
            return True, text, False
        # non-owner directive -> demote to observation, keep as behavioral fact
        demoted = f"[observed] this person asked the assistant to: {text.strip()}"
        return True, demoted, True
    except Exception:
        return True, text, False


def gate_world_fact(text, is_owner_src):
    """Gate a WORLD fact (extract_memory lane). World-facts are system-scope by
    nature (they become general standing knowledge/rules). So:
      - owner source: pass untouched.
      - non-owner + personalization-shaped: pass (rare here, but harmless data).
      - non-owner + directive: DROP. A non-owner cannot install a standing
        system rule via world-memory. (Not demoted-in-place because world-facts
        have no person to attribute the observation to; the peer lane already
        captured it as an observation if relevant.)
    Fail-open: on any error, keep original."""
    try:
        if is_owner_src or classify_scope(text) == "personalization":
            return True, text, False
        return False, text, True  # non-owner system-directive -> not stored
    except Exception:
        return True, text, False
