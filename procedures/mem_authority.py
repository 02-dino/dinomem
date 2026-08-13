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
# Owner id is resolved through a SMOOTH CHAIN so a non-technical installer gets
# working security with zero manual steps whenever the info already exists on the
# box, and only falls back to "ask" when it genuinely can't be found:
#   1. DINOMEM_OWNER_IDS env         (explicit override — installer/operator set)
#   2. DINOTRUST_OWNER_IDS env        (dinotrust user -> free)
#   3. dinotrust `owner_ids:` parsed from openclaw.json  (dinotrust installed ->
#      free, always in sync, no duplicate config to maintain)
#   4. ~/.dinomem/owner_ids cache file  (what the installer writes after asking)
#   5. NONE -> is_owner() fail-opens to passthrough (no over-filtering); a
#      one-time nudge (owner_gate_nudge) tells the operator the gate is inactive.
# Every step is fail-open: an error in one source never blocks the next, and
# total failure yields an empty set. Memoized (owner config changes rarely).
_OWNER_CACHE = None  # None = unresolved; set() (possibly empty) once resolved


def _parse_id_blob(raw):
    """Comma/whitespace-separated id blob -> clean set of str ids. Fail-open."""
    try:
        return {x.strip() for x in re.split(r"[,\s]+", str(raw)) if x.strip()}
    except Exception:
        return set()


def _openclaw_config_paths():
    """Candidate openclaw.json locations (env override first, then defaults)."""
    paths = []
    for ev in ("OPENCLAW_CONFIG", "OPENCLAW_HOME"):
        v = os.environ.get(ev)
        if v:
            v = os.path.expanduser(v)
            paths.append(v if v.endswith(".json") else os.path.join(v, "openclaw.json"))
    home = os.path.expanduser("~")
    paths.append(os.path.join(home, ".openclaw", "openclaw.json"))
    paths.append("/root/.openclaw/openclaw.json")
    seen, out = set(), []
    for p in paths:
        if p and p not in seen:
            seen.add(p); out.append(p)
    return out


def _ids_from_dinotrust_config():
    """Parse dinotrust's injected `owner_ids:` line out of openclaw.json.
    dinotrust stores owners as a YAML line inside a string-valued config field,
    so we regex the raw file text rather than assume a fixed JSON path.
    Fail-open -> empty set."""
    for path in _openclaw_config_paths():
        try:
            if not os.path.exists(path):
                continue
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                blob = f.read()
            m = re.search(r"owner_ids:\s*(\[[^\]]*\]|[0-9][0-9,\s\\\"']*)", blob)
            if m:
                ids = _parse_id_blob(re.sub(r'[\[\]"\'\\]', " ", m.group(1)))
                ids = {i for i in ids if i.isdigit()}
                if ids:
                    return ids
        except Exception:
            continue
    return set()


def _ids_from_cache_file():
    """Read the installer-written owner-id cache. Fail-open.

    MULTI-AGENT: ~/.dinomem/owner_ids is HOME-global — one file shared by every
    agent on the host, so two agents with DIFFERENT owners would clobber each
    other. Prefer a PER-AGENT file ~/.dinomem/owner_ids.<agentId> (written by the
    installer with the agent's own owner) whenever DINOMEM_AGENT_ID is set, then
    fall back to the global file (single-agent hosts, legacy installs). An
    explicit DINOMEM_OWNER_FILE override still wins over both."""
    try:
        override = os.environ.get("DINOMEM_OWNER_FILE", "").strip()
        candidates = []
        if override:
            candidates.append(override)
        else:
            aid = (os.environ.get("DINOMEM_AGENT_ID", "") or "").strip().lower()
            if aid:
                candidates.append("~/.dinomem/owner_ids." + aid)
            candidates.append("~/.dinomem/owner_ids")
        for cand in candidates:
            path = os.path.expanduser(cand)
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    ids = _parse_id_blob(f.read())
                if ids:
                    return ids
    except Exception:
        pass
    return set()


def _agent_specific_cache_ids():
    """Owner ids from a PER-AGENT cache file ONLY (~/.dinomem/owner_ids.<agentId>
    or an explicit DINOMEM_OWNER_FILE). Empty if there is no agent-specific file.
    Kept separate from the global-file read so an agent-specific owner can
    OUTRANK the host's ambient dinotrust config (see _owner_ids ordering)."""
    try:
        override = os.environ.get("DINOMEM_OWNER_FILE", "").strip()
        if override:
            p = os.path.expanduser(override)
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    return _parse_id_blob(f.read())
            return set()
        aid = (os.environ.get("DINOMEM_AGENT_ID", "") or "").strip().lower()
        if aid:
            p = os.path.expanduser("~/.dinomem/owner_ids." + aid)
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    return _parse_id_blob(f.read())
    except Exception:
        pass
    return set()


def _owner_ids():
    """Resolve owner ids via the smooth chain (memoized, fail-open).

    ORDER (multi-agent correct):
      1. DINOMEM_OWNER_IDS / DINOTRUST_OWNER_IDS env  (explicit, per-process)
      2. PER-AGENT cache file (owner_ids.<agentId> / DINOMEM_OWNER_FILE) — an
         agent explicitly configured for a specific owner OUTRANKS the host's
         ambient dinotrust config, so agent B on a shared host is not silently
         assigned host-owner A.
      3. dinotrust owner_ids: from openclaw.json  (host primary owner)
      4. global ~/.dinomem/owner_ids cache file  (legacy single-agent)
    """
    global _OWNER_CACHE
    if _OWNER_CACHE is not None:
        return _OWNER_CACHE
    ids = set()
    try:
        ids = _parse_id_blob(os.environ.get("DINOMEM_OWNER_IDS", ""))
        if not ids:
            ids = _parse_id_blob(os.environ.get("DINOTRUST_OWNER_IDS", ""))
        if not ids:
            ids = _agent_specific_cache_ids()   # per-agent file beats host config
        if not ids:
            ids = _ids_from_dinotrust_config()
        if not ids:
            ids = _ids_from_cache_file()        # global file (legacy fallback)
    except Exception:
        ids = set()
    _OWNER_CACHE = ids
    return ids


def owner_config_present():
    """True if ANY owner id was resolved. The extractor can call this once to
    decide whether to emit the one-time 'gate inactive' nudge."""
    return bool(_owner_ids())


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
