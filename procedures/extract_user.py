#!/usr/bin/env python3
"""
extract_user.py — Peer Representation Deriver (BASE tier)

Derives per-person representations (Honcho-style) from archived DM sessions and
writes them to memory/peers/<platform>_<platform_id>.md (composite key).

Design (all locked in _note_peer-representation-honcho-port.md):
  - Reuses extract_memory.py plumbing: Node self-heal, call_llm gateway->OpenRouter
    fallback, processed-log dedup, file lock, archive scan.
  - ONE scanner, person-extraction head (world-facts stay in extract_memory.py).
  - Two-stage surprisal gate: Stage-0 cheap pattern scan (recall-max, false-pos OK)
    -> Stage-1 LLM derive (quality + novelty). Contradiction = max surprisal.
  - Supersede-in-place with provenance timestamp; staleness = confidence decay.
  - Creation: stub-always (cheap) / derive-gated (rich derive only on 2nd+ interaction).
  - Composite key <platform>_<platform_id> — NEVER bare id (ids collide across platforms).
  - FAIL-OPEN: any error -> skip that archive, never break the memory pipeline.
  - stdlib only (noob-install-clean). No graph, no relations (that's NEURON).

Run via orchestrator (auto_session_reset.py, two-heads on same archive scan) or standalone.
Logs to: logs/extract_user.log
"""

import json
import os
import re
import glob as _glob
import subprocess
import sys
import fcntl
import urllib.request
from pathlib import Path
from datetime import datetime, timedelta

# ─── Reuse extract_memory.py plumbing (import; fail-open if unavailable) ────────
# extract_user is a SECOND HEAD on the same archive scan. It imports the already-
# hardened helpers from extract_memory rather than re-implementing fragile logic
# (Node self-heal, call_llm gateway->OpenRouter fallback, archive content split).
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
try:
    import extract_memory as _em
    _EM_OK = True
except Exception:
    _em = None
    _EM_OK = False

# Authority-scope gate (provenance != authority). Fail-open: if the module is
# missing, gate_* become identity passthroughs so extraction never breaks.
try:
    import mem_authority as _auth
    _AUTH_OK = True
except Exception:
    _auth = None
    _AUTH_OK = False

def _gate_peer(text, is_owner_src):
    """Wrap mem_authority.gate_peer_fact; passthrough if module unavailable."""
    if not _AUTH_OK:
        return True, text, False
    try:
        return _auth.gate_peer_fact(text, is_owner_src)
    except Exception:
        return True, text, False

# ─── Configuration ──────────────────────────────────────────────────
SESSIONS_DIR = Path("DINOMEM_AGENT_SESSIONS_PLACEHOLDER")  # rewritten at install (mirrors extract_memory)
MEMORY_DIR = _HERE.parent / "memory"
PEERS_DIR = MEMORY_DIR / "peers"
LOG_FILE = _HERE.parent / "logs" / "extract_user.log"
PROCESSED_LOG = _HERE.parent / "logs" / ".extract_user_processed.json"
STATUS_FILE = _HERE.parent / "logs" / ".extract_user_status.json"
EXTRACT_LOCK_FILE = Path("/tmp/dinomem_extract_user.lock")
TEMPLATE_FILE = _HERE.parent / "templates" / "peer_rep.md.tmpl"

# Rich-derive gate: stub on 1st contact, only derive on >= this many interactions.
DERIVE_MIN_INTERACTIONS = 2
# Batch of archives per run (inherit extract_memory cadence).
BATCH_SIZE = 3
# Confidence decay applied to untouched facts per compaction pass (staleness).
DECAY_PER_PASS = 0.02
# Floor below which a decayed fact is dropped by the peers-scoped compactor.
DECAY_FLOOR = 0.15

# Ensure dirs exist (fail-open).
for _d in (LOG_FILE.parent, PEERS_DIR):
    try:
        _d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass


# ═══ LOGGING + LLM (delegate to extract_memory; fail-open fallback) ═══════════
def log(msg):
    """Timestamped log line. Never raises."""
    line = f"[{datetime.now().isoformat()}] {msg}"
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    try:
        print(line, flush=True)
    except Exception:
        pass


# ─── OVERWRITE-SAFETY: neuron's installer force-overwrites procedures/extract_memory.py
# (ALWAYS_OVERWRITE) with a SUPERSET. base extract_user.py imports whatever copy is
# present, so we must tolerate signature/return-shape DRIFT between base and neuron:
#   base:   call_llm(prompt, max_tokens=None, reasoning=False)          # 3 params
#   neuron: call_llm(prompt, max_tokens=None, reasoning=False, model=None)  # +model
# We call only the COMMON subset (never pass `model`) + inspect the live signature
# and degrade gracefully, so a future neuron API change can't silently break base.
def _em_supports_kwarg(fn, name):
    """True if callable `fn` accepts keyword `name`. Fail-open: False on any error."""
    try:
        import inspect
        sig = inspect.signature(fn)
        params = sig.parameters
        if name in params:
            return True
        # a **kwargs sink also accepts it
        return any(p.kind == p.VAR_KEYWORD for p in params.values())
    except Exception:
        return False


def call_llm(prompt, max_tokens=None, reasoning=False):
    """Person-deriver LLM call. Delegates to extract_memory.call_llm (gateway ->
    OpenRouter fallback, cheap/reasoning routing) so we inherit its hardening.
    reasoning=False by default (cheap deriver). Returns (success, text).
    OVERWRITE-SAFE: probes the live signature; only passes kwargs it accepts, and
    falls back positionally if the drifted signature rejects our kwargs."""
    if not (_EM_OK and hasattr(_em, "call_llm")):
        log("   ⚠️  extract_memory unavailable — cannot derive (fail-open skip)")
        return False, "extract_memory.call_llm unavailable"
    fn = _em.call_llm
    try:
        kwargs = {}
        if _em_supports_kwarg(fn, "max_tokens"):
            kwargs["max_tokens"] = max_tokens
        if _em_supports_kwarg(fn, "reasoning"):
            kwargs["reasoning"] = reasoning
        # deliberately NEVER pass `model` — not in base's signature; let neuron default.
        return fn(prompt, **kwargs)
    except TypeError as e:
        # Signature drifted past our probe — retry positional-minimal, then bare.
        log(f"   ⚠️  call_llm signature drift ({e}) — retrying positional")
        try:
            return fn(prompt, max_tokens)
        except Exception:
            try:
                return fn(prompt)
            except Exception as e2:
                log(f"   ⚠️  call_llm positional fallback failed: {e2}")
                return False, f"call_llm error: {e2}"
    except Exception as e:
        log(f"   ⚠️  extract_memory.call_llm raised: {e}")
        return False, f"call_llm error: {e}"


def sanitize_text(text):
    if not isinstance(text, str):
        return ""
    return text.encode("utf-8", "ignore").decode("utf-8")


# ═══ PROCESSED-LOG DEDUP (own log; do not clobber extract_memory's) ══════════
def load_processed_set():
    if PROCESSED_LOG.exists():
        try:
            with open(PROCESSED_LOG, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception as e:
            log(f"   ⚠️  Error loading processed log: {e}")
            return set()
    return set()


def save_processed_set(processed_set):
    try:
        with open(PROCESSED_LOG, "w", encoding="utf-8") as f:
            json.dump(sorted(list(processed_set)), f, indent=2)
    except Exception as e:
        log(f"   ⚠️  Error saving processed log: {e}")


def write_status(ok, remaining, note=""):
    """Machine-readable status for the orchestrator. Never raises."""
    try:
        payload = {
            "ok": bool(ok),
            "remaining_backlog": int(remaining),
            "note": note,
            "updated_at": datetime.now().isoformat(),
        }
        with open(STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f)
    except Exception:
        pass


# ═══ SESSION-KEY -> COMPOSITE PEER KEY ════════════════════════════
# DM session keys end in the sender id, e.g.:
#   agent:<name>:telegram:direct:1083618205   -> platform=telegram id=1083618205
# Group keys (agent:...:group:-100...:topic:N) are NOT per-person -> skip in BASE.
_DM_KEY_RE = re.compile(r":([a-z0-9_-]+):direct:([^:]+)$", re.IGNORECASE)


def parse_peer_key(session_key):
    """Return (platform, platform_id) for a DM session key, else (None, None).
    Group/topic keys return (None, None) -> caller skips (unattributable in BASE)."""
    if not session_key:
        return None, None
    m = _DM_KEY_RE.search(str(session_key))
    if not m:
        return None, None
    return m.group(1).lower(), m.group(2)


def composite_key(platform, platform_id):
    return f"{platform}_{platform_id}"


def peer_path(platform, platform_id):
    return PEERS_DIR / f"{composite_key(platform, platform_id)}.md"

# ═══ PEER GRAPH (NEURON — SEPARATE from global memory_graph.json) ═════════════
# Privacy-by-construction (OQ6): peer edges NEVER touch the global graph. This is
# a standalone JSON keyed by namespaced subject `peer:<platform>:<id>`. A scoped
# graph_search reads it and enforces active-speaker isolation; cross-peer
# aggregation is OWNER-ONLY. Fail-open: any error leaves the graph untouched.
PEERS_GRAPH_FILE = PEERS_DIR / "peers_graph.json"
PEERS_GRAPH_LOCK = Path("/tmp/dinomem_peers_graph.lock")

def _peer_subject(platform, platform_id):
    """Namespaced subject id. The `peer:` prefix is the privacy filter key."""
    return f"peer:{platform}:{platform_id}"

def _load_peers_graph():
    """Return the peers graph dict, or a fresh empty skeleton. Fail-open."""
    skel = {"version": 1, "kind": "peers_graph", "nodes": {}, "edges": []}
    try:
        if PEERS_GRAPH_FILE.exists():
            data = json.loads(PEERS_GRAPH_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "edges" in data:
                data.setdefault("nodes", {})
                data.setdefault("version", 1)
                data.setdefault("kind", "peers_graph")
                return data
    except Exception as e:
        log(f"   ⚠️  peers_graph load failed ({e}); starting fresh")
    return skel

def _atomic_write_json(path, data):
    """Write JSON atomically (tmp + rename). Fail-open (returns False)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except Exception as e:
        log(f"   ⚠️  atomic write failed ({path.name}): {e}")
        return False

def upsert_peer_edges(platform, platform_id, relations, ts):
    """Merge derived relation edges into peers_graph.json under a flock.
    relations: list of {verb, object, confidence}. Subject is ALWAYS this peer
    (namespaced) — privacy-by-construction. Dedup on (subject, verb, object).
    Fail-open: any failure leaves the graph unchanged and never breaks derive.
    Returns count of edges added/updated (0 on no-op or failure)."""
    rels = [r for r in (relations or []) if isinstance(r, dict)
            and sanitize_text(r.get("object", "")).strip()
            and sanitize_text(r.get("verb", "")).strip()]
    if not rels:
        return 0
    subj = _peer_subject(platform, platform_id)
    lock_fh = None
    try:
        import fcntl
        lock_fh = open(PEERS_GRAPH_LOCK, "w")
        fcntl.flock(lock_fh, fcntl.LOCK_EX)
    except Exception:
        lock_fh = None  # fail-open: proceed without lock rather than drop edges
    try:
        g = _load_peers_graph()
        g["nodes"].setdefault(subj, {"type": "peer", "platform": platform,
                                     "platform_id": str(platform_id)})
        index = {(e.get("subject"), e.get("verb"), e.get("object")): e
                 for e in g["edges"]}
        n = 0
        for r in rels:
            verb = sanitize_text(r.get("verb", "")).strip().lower()
            obj = sanitize_text(r.get("object", "")).strip()
            conf = r.get("confidence", 0.7)
            try:
                conf = float(conf)
            except Exception:
                conf = 0.7
            obj_node = f"obj:{obj.lower()}"
            g["nodes"].setdefault(obj_node, {"type": "object", "label": obj})
            key = (subj, verb, obj)
            if key in index:
                e = index[key]
                e["confidence"] = max(e.get("confidence", 0.0), conf)
                e["last_seen"] = ts
                e["count"] = e.get("count", 1) + 1
            else:
                edge = {"subject": subj, "verb": verb, "object": obj,
                        "object_node": obj_node, "confidence": conf,
                        "first_seen": ts, "last_seen": ts, "count": 1}
                g["edges"].append(edge)
                index[key] = edge
            n += 1
        if n:
            _atomic_write_json(PEERS_GRAPH_FILE, g)
        return n
    except Exception as e:
        log(f"   ⚠️  upsert_peer_edges failed: {e}")
        return 0
    finally:
        if lock_fh is not None:
            try:
                import fcntl
                fcntl.flock(lock_fh, fcntl.LOCK_UN)
                lock_fh.close()
            except Exception:
                pass


# ═══ ARCHIVE CONTENT (delegate to extract_memory's hardened splitter) ════════
def read_archive_meta_and_text(filename):
    """Return (session_key, combined_text) for an archive.
    - text via extract_memory.extract_single_archive_content (compaction-aware,
      role-tagged, session-boundary chunked) so we share ONE disk read format.
    - session_key parsed from the archive's session record (id/cwd carry it in
      newer formats; fall back to the filename's session uuid otherwise).
    Fail-open: on any error returns (None, "")."""
    text = ""
    if _EM_OK and hasattr(_em, "extract_single_archive_content"):
        try:
            res = _em.extract_single_archive_content(filename)
            # extract_memory may return a str or a list of chunks; normalize.
            if isinstance(res, (list, tuple)):
                text = "\n\n".join(str(c) for c in res if c)
            elif res:
                text = str(res)
        except Exception as e:
            log(f"   ⚠️  archive content extract failed ({filename}): {e}")
            text = ""
    session_key = _session_key_from_archive(filename)
    return session_key, text


# VERIFIED (2026-07-24 smoke test): the DM sender id is NOT in the archive's
# `session` record (that only has id/cwd/timestamp). It appears inside MESSAGE
# bodies, in sourceMessageId strings like:
#   telegram-final:agent:analyst:telegram:direct:829441784:829441784:10590:0
# So we scan the WHOLE archive for the platform:direct:<id> pattern, not just the
# session record. Group keys (:group:...:topic:) are intentionally NOT matched
# (unattributable per-person in BASE). Fail-open: None if unrecoverable.
_ARCHIVE_DM_RE = re.compile(r"([a-z0-9_-]+):direct:([0-9]+)", re.IGNORECASE)


def _session_key_from_archive(filename):
    """Best-effort DM peer-key recovery from an archive. Returns a synthetic
    session-key string 'agent:_:<platform>:direct:<id>' (parse_peer_key-compatible)
    or None. Prefers an explicit sessionKey in the session record if present,
    else scans message bodies for the platform:direct:<id> signature."""
    try:
        fp = SESSIONS_DIR / filename
        # First pass: explicit session-key field (newer formats may carry it).
        with open(fp, "r", encoding="utf-8") as f:
            head = 0
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("type") == "session":
                    for k in ("sessionKey", "session_key", "key"):
                        v = d.get(k)
                        if isinstance(v, str) and ":direct:" in v:
                            return v
                head += 1
                if head > 5:
                    break
        # Second pass: scan message bodies for platform:direct:<id> (the real source).
        # Count occurrences per (platform,id); pick the dominant peer (a DM archive
        # should be single-peer, but reset/orphan merges can be defensive here).
        counts = {}
        with open(fp, "r", encoding="utf-8") as f:
            for line in f:
                for m in _ARCHIVE_DM_RE.finditer(line):
                    plat = m.group(1).lower()
                    pid = m.group(2)
                    # skip obvious non-platform tokens (the id repeats as chat:sender)
                    if plat in ("final", "agent", "analyst"):
                        continue
                    counts[(plat, pid)] = counts.get((plat, pid), 0) + 1
        if counts:
            (plat, pid), _n = max(counts.items(), key=lambda kv: kv[1])
            return f"agent:_:{plat}:direct:{pid}"
    except Exception:
        pass
    return None


# ═══ TWO-STAGE SURPRISAL GATE ═════════════════════════════════
# Stage-0 = cheap pattern scan. Recall-maximizing: false-POSITIVES are fine
# (they just cost a Stage-1 derive), false-NEGATIVES are the enemy (silent miss).
# Only PROVABLY-empty content skips. Fires on any signal-TYPE marker.
_SIGNAL_PATTERNS = [
    # stated preference / instruction
    r"\bi (?:prefer|like|want|need|hate|don't want|do not want|always|never)\b",
    r"\b(?:please )?(?:just|only|always|never) (?:give|send|show|do)\b",
    # identity / self-fact
    r"\bi(?:'m| am)\b", r"\bmy (?:name|handle|risk|budget|portfolio|style|beat)\b",
    r"\bi (?:trade|hold|run|work|use|build|manage)\b",
    # correction / pushback (high surprisal)
    r"\b(?:no,|actually|that's wrong|incorrect|not what i|you're wrong|wrong\b)",
    r"\bstop\b", r"\bdon't\b",
    # risk / money markers (domain-salient identity)
    r"\b(?:money i can(?:'| )?(?:not|t) lose|can afford to lose|risk (?:appetite|tolerance))\b",
    # tonal / relationship markers
    r"\b(?:lol|thanks|appreciate|frustrat|annoy)\w*\b",
]
_SIGNAL_RE = re.compile("|".join(_SIGNAL_PATTERNS), re.IGNORECASE)


def stage0_has_signal(text):
    """Cheap recall-max scan. Returns True unless PROVABLY empty of any signal.
    Bias: when unsure, return True (let Stage-1 judge). Only a genuinely empty /
    pure-machinery transcript returns False."""
    if not text or not text.strip():
        return False
    # Any user-authored line at all with a signal marker -> pass to Stage-1.
    if _SIGNAL_RE.search(text):
        return True
    # No marker matched, but non-trivial user content still may carry a fact the
    # cheap patterns don't cover. Recall-max: pass if there's meaningful [USER] text.
    user_chars = 0
    for line in text.splitlines():
        if line.strip().upper().startswith("[USER]") or line.strip().startswith("[USER]:"):
            user_chars += len(line)
    return user_chars >= 40  # a couple sentences of user speech -> worth a look


# ═══ REP READ / EXISTS ═════════════════════════════════════
def load_rep(platform, platform_id):
    """Return existing rep text or None."""
    p = peer_path(platform, platform_id)
    if p.exists():
        try:
            return p.read_text(encoding="utf-8")
        except Exception:
            return None
    return None


def rep_interactions(rep_text):
    """Parse the interaction count from rep frontmatter (0 if absent)."""
    if not rep_text:
        return 0
    m = re.search(r"^interactions:\s*(\d+)", rep_text, re.MULTILINE)
    return int(m.group(1)) if m else 0


# ═══ STAGE-1 DERIVE PROMPT (person facts + theory-of-mind) ══════════════
DERIVE_PROMPT = """You are deriving a PERSON REPRESENTATION from a chat transcript between an
assistant and ONE person. Extract ONLY facts about WHO THIS PERSON IS — their
stated preferences, revealed behavior, identity facts, risk appetite, domain,
tone. Do NOT extract world-facts (market data, general knowledge) — those belong
elsewhere.

Existing representation (may be empty):
---
{existing}
---

New transcript ([USER] = the person, [ASSISTANT] = the bot):
---
{transcript}
---

Return STRICT JSON (no markdown fence), shape:
{{
  "facts": [{{"text": "<stated/identity fact>", "confidence": 0.0-1.0}}],
  "beat": [{{"text": "<revealed-behavior / theory-of-mind INFERENCE>", "confidence": 0.0-1.0}}],
  "ledger": ["<their call/outcome as observed, if any>"],
  "relations": [{{"verb": "<relationship verb>", "object": "<the thing/asset/concept they relate to>", "confidence": 0.0-1.0}}],
  "supersedes": [{{"old": "<existing line this contradicts>", "new": "<corrected fact>"}}]
}}

Rules:
- Empty arrays are VALID and expected when nothing new/salient is present. Do NOT invent.
- "facts" = DURABLE stated/identity facts about THIS person's own life: education, occupation/employer, location, relationships/family, health/allergies, possessions, personal history/biography. Capture on FIRST mention (do not wait for repetition). First-person cues: "I graduated in X", "I work at Y", "I live in Z", "my wife/sister/son...", "I'm allergic to...", "I switched jobs to...". These are HARD facts (go in "facts"), distinct from theory-of-mind inferences ("beat") and typed relations. A person's biography is high-value recall — err on the side of extracting durable life-facts.
- confidence: stated-by-person=0.9+, strong inference=0.7, weak inference=0.5.
- A CORRECTION/contradiction of an existing line -> put it in "supersedes".
- Theory-of-mind inferences ("prefers raw data", "low risk tolerance") go in "beat" with confidence, never as hard facts.
- Never write facts about anyone OTHER than this person.
- "relations" (NEURON): typed edges FROM this person TO a thing/asset/concept. verb = the relationship (e.g. "trades", "holds", "asks_about", "prefers", "distrusts"), object = the target (e.g. "ETH", "raw orderbook data", "leverage"). ONLY clear, non-trivial edges. Max 4. The subject is ALWAYS this person -- never emit an edge about anyone else. Skip if nothing clear.
"""


# ═══ STUB CREATION (cheap, always on first contact) ══════════════════
def ensure_stub(platform, platform_id, name=None, handle=None):
    """Create a minimal stub rep on first contact if none exists. Cheap: no LLM.
    Uses the template if present, else a minimal inline schema. Fail-open."""
    p = peer_path(platform, platform_id)
    if p.exists():
        return False
    today = datetime.now().strftime("%Y-%m-%d")
    tmpl = None
    if TEMPLATE_FILE.exists():
        try:
            tmpl = TEMPLATE_FILE.read_text(encoding="utf-8")
        except Exception:
            tmpl = None
    if tmpl:
        body = (tmpl
                .replace("PLATFORM_ID", str(platform_id))
                .replace("PLATFORM", platform)
                .replace("NAME", name or "unknown")
                .replace("HANDLE", handle or "")
                .replace("FIRST_SEEN", today)
                .replace("LAST_UPDATED", today))
        # add interactions field if template lacks it
        if "interactions:" not in body:
            body = body.replace("last_updated: " + today,
                                "last_updated: " + today + "\ninteractions: 1", 1)
    else:
        body = (f"---\ntype: peer\nplatform: {platform}\nplatform_id: \"{platform_id}\"\n"
                f"person_ref:\nname: {name or 'unknown'}\nhandle: {handle or ''}\n"
                f"first_seen: {today}\nlast_updated: {today}\ninteractions: 1\ntags: []\n---\n\n"
                f"# Peer Rep — {name or platform_id} ({platform}:{platform_id})\n\n"
                f"## FACTS\n\n## BEAT\n\n## RELATIONS\n\n## LEDGER\n\n## PROVENANCE\n")
    try:
        p.write_text(body, encoding="utf-8")
        log(f"   🆕 stub rep created: {p.name}")
        return True
    except Exception as e:
        log(f"   ⚠️  stub create failed ({p.name}): {e}")
        return False


def bump_interactions(platform, platform_id):
    """Increment the interaction counter in the rep frontmatter. Fail-open.
    Returns the new count (0 on failure)."""
    p = peer_path(platform, platform_id)
    if not p.exists():
        return 0
    try:
        txt = p.read_text(encoding="utf-8")
        cur = rep_interactions(txt)
        new = cur + 1
        if re.search(r"^interactions:\s*\d+", txt, re.MULTILINE):
            txt = re.sub(r"^interactions:\s*\d+", f"interactions: {new}", txt, count=1, flags=re.MULTILINE)
        else:
            txt = re.sub(r"^(last_updated:.*)$", r"\1\ninteractions: " + str(new), txt, count=1, flags=re.MULTILINE)
        p.write_text(txt, encoding="utf-8")
        return new
    except Exception:
        return 0


# ═══ APPLY DERIVE RESULT (supersede-in-place + append) ═════════════════
def _append_under(section_body, header, lines):
    """Insert `lines` (list of str) under `## header` in section_body. If the
    header is missing, append the header + lines at end. Returns new text."""
    if not lines:
        return section_body
    block = "\n".join(lines)
    pat = re.compile(r"(^## " + re.escape(header) + r"\s*\n)", re.MULTILINE)
    m = pat.search(section_body)
    if m:
        insert_at = m.end()
        return section_body[:insert_at] + block + "\n" + section_body[insert_at:]
    return section_body.rstrip() + f"\n\n## {header}\n{block}\n"


def apply_derive(platform, platform_id, derived):
    """Write derived facts/beat/ledger/supersedes into the rep. Supersede-in-place
    with provenance; append new facts. Fail-open (returns False on any error).

    AUTHORITY-SCOPE GATE: this rep belongs to `platform_id`. If that id is NOT an
    owner, any derived line that reads as a SYSTEM/ASSISTANT DIRECTIVE (not a fact
    about the person) is DEMOTED to a neutral observation — NEVER dropped, NEVER
    tagged 'untrusted'. Personalization facts pass untouched, fully trusted."""
    p = peer_path(platform, platform_id)
    today = datetime.now().strftime("%Y-%m-%d")
    _is_owner_src = _auth.is_owner(platform_id) if _AUTH_OK else True
    try:
        txt = p.read_text(encoding="utf-8") if p.exists() else ""
    except Exception:
        txt = ""
    if not txt:
        ensure_stub(platform, platform_id)
        try:
            txt = p.read_text(encoding="utf-8")
        except Exception:
            return False

    changed = False

    # 1) Supersede-in-place: replace old line, log provenance.
    for sup in (derived.get("supersedes") or []):
        old = sanitize_text(sup.get("old", "")).strip()
        new = sanitize_text(sup.get("new", "")).strip()
        if not old or not new:
            continue
        if old in txt:
            txt = txt.replace(old, f"{new}  (conf: 0.9, ts: {today})", 1)
            prov = f"- [SUPERSEDED {today}] {old} -> {new}"
            txt = _append_under(txt, "PROVENANCE", [prov])
            changed = True

    # 2) Append new FACTS / BEAT / LEDGER (dedup on exact text).
    fact_lines = []
    for f in (derived.get("facts") or []):
        t = sanitize_text(f.get("text", "")).strip()
        c = f.get("confidence", 0.7)
        _keep, t, _dem = _gate_peer(t, _is_owner_src)
        if _dem:
            log(f"   🧱 authority-gate: demoted non-owner directive to observation ({p.name})")
        if t and t not in txt:
            fact_lines.append(f"- {t}  (conf: {c}, ts: {today})")
    beat_lines = []
    for b in (derived.get("beat") or []):
        t = sanitize_text(b.get("text", "")).strip()
        c = b.get("confidence", 0.6)
        _keep, t, _dem = _gate_peer(t, _is_owner_src)
        if t and t not in txt:
            beat_lines.append(f"- {t}  (conf: {c}, ts: {today})")
    ledger_lines = []
    for l in (derived.get("ledger") or []):
        t = sanitize_text(l).strip()
        if t and t not in txt:
            ledger_lines.append(f"- {t}  (ts: {today})")

    # NEURON: RELATIONS tier. Write namespaced edge lines into the rep's
    # RELATIONS section (human-readable) AND upsert into peers_graph.json
    # (traversable). Dedup on exact edge line in-file.
    rel_lines = []
    subj = _peer_subject(platform, platform_id)
    for r in (derived.get("relations") or []):
        if not isinstance(r, dict):
            continue
        verb = sanitize_text(r.get("verb", "")).strip().lower()
        obj = sanitize_text(r.get("object", "")).strip()
        if not verb or not obj:
            continue
        c = r.get("confidence", 0.7)
        line = f"- {subj} → {verb} → {obj}  (conf: {c}, ts: {today})"
        if line.split("  (conf")[0] not in txt:
            rel_lines.append(line)

    if fact_lines:
        txt = _append_under(txt, "FACTS", fact_lines); changed = True
    if beat_lines:
        txt = _append_under(txt, "BEAT", beat_lines); changed = True
    if ledger_lines:
        txt = _append_under(txt, "LEDGER", ledger_lines); changed = True
    if rel_lines:
        txt = _append_under(txt, "RELATIONS", rel_lines); changed = True

    if changed:
        txt = re.sub(r"^last_updated:.*$", f"last_updated: {today}", txt, count=1, flags=re.MULTILINE)
        try:
            p.write_text(txt, encoding="utf-8")
            log(f"   ✍️  rep updated: {p.name} (+{len(fact_lines)}F +{len(beat_lines)}B +{len(ledger_lines)}L +{len(rel_lines)}R)")
        except Exception as e:
            log(f"   ⚠️  rep write failed ({p.name}): {e}")
            return False

    # Upsert edges into the SEPARATE peers_graph.json (privacy-by-construction).
    # Fail-open: graph failure never blocks the rep write above.
    try:
        added = upsert_peer_edges(platform, platform_id, derived.get("relations"), today)
        if added:
            log(f"   🔗 peers_graph: +{added} edge(s) for {subj}")
    except Exception as e:
        log(f"   ⚠️  peers_graph upsert skipped ({e})")

    return changed


def parse_derive_json(raw):
    """Extract the JSON object from an LLM response. Tolerant: strips fences,
    finds the first {...} block. Returns {} on failure (fail-open)."""
    if not raw:
        return {}
    s = raw.strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.MULTILINE).strip()
    try:
        return json.loads(s)
    except Exception:
        pass
    m = re.search(r"\{.*\}", s, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return {}
    return {}


# ═══ PER-ARCHIVE PIPELINE ══════════════════════════════════
def process_archive(filename):
    """Full per-archive flow. Fail-open: returns True (processed) even on skip so
    the archive is not retried forever; logs the reason. Only a genuine
    infrastructure error returns False (retry next run)."""
    session_key, text = read_archive_meta_and_text(filename)
    platform, platform_id = parse_peer_key(session_key)
    if not platform or not platform_id:
        log(f"   ⏭️  {filename}: no DM peer key (group/unattributable) — skip")
        return True

    # Stub-always: cheap creation on first contact (no LLM).
    ensure_stub(platform, platform_id)
    interactions = bump_interactions(platform, platform_id)

    # Derive-gated: only pay for a rich derive on 2nd+ meaningful interaction.
    if interactions < DERIVE_MIN_INTERACTIONS:
        log(f"   ⏸️  {composite_key(platform, platform_id)}: interaction {interactions} < {DERIVE_MIN_INTERACTIONS} — stub only")
        return True

    # Stage-0 surprisal gate (cheap, recall-max).
    if not stage0_has_signal(text):
        log(f"   ⏸️  {composite_key(platform, platform_id)}: stage-0 empty of signal — skip derive")
        return True

    # Stage-1 LLM derive (quality + novelty).
    existing = load_rep(platform, platform_id) or "(none)"
    prompt = DERIVE_PROMPT.format(existing=existing[:4000], transcript=text[:12000])
    ok, raw = call_llm(prompt, max_tokens=1200, reasoning=False)
    if not ok:
        log(f"   ⚠️  {composite_key(platform, platform_id)}: derive LLM failed — fail-open skip")
        return True  # fail-open: don't block pipeline, don't retry forever
    derived = parse_derive_json(raw)
    if not derived or not any(derived.get(k) for k in ("facts", "beat", "ledger", "supersedes")):
        log(f"   ∅ {composite_key(platform, platform_id)}: derive returned nothing salient (valid)")
        return True
    apply_derive(platform, platform_id, derived)
    return True


# ═══ PEERS-SCOPED COMPACTOR (NEURON — decay, never age-delete) ═════════════
# Confidence DECAY on staleness, NEVER deletion of the rep file. Each pass:
#   - facts/beat lines carrying (conf: X, ts: Y): if the line was NOT touched this
#     pass, decay conf by DECAY_PER_PASS. Below DECAY_FLOOR -> move to PROVENANCE
#     as [DECAYED], do NOT hard-delete (audit trail preserved).
#   - LEDGER + RELATIONS lines are NOT decayed (events/edges are durable history).
#   - The rep FILE is never deleted (owner-purge only). Empty rep stays as a stub.
_CONF_TS_RE = re.compile(r"\(conf:\s*([0-9.]+)\s*,\s*ts:\s*([0-9-]+)\)")
_DECAYED_PREFIX_RE = re.compile(r"^(?:\[DECAYED [0-9-]+\]\s*)+")
# The rep's canonical section order. compact_rep REBUILDS from this so structure
# can never drift (no duplicate headers, blank line after each header preserved).
_SECTION_ORDER = ["FACTS", "BEAT", "RELATIONS", "LEDGER", "PROVENANCE"]

def _decay_section(body, today, decayed_out):
    """Decay every '(conf: X, ts: Y)' line in a FACTS/BEAT section body. Lines whose
    ts == today are fresh (not decayed). Below-floor lines are removed and their
    CLEANED text (no old [DECAYED] prefix, no conf tag) is pushed to decayed_out.
    Returns (kept_line_list, changed)."""
    kept, changed = [], False
    for ln in body.splitlines():
        if not ln.strip():
            continue  # drop blank lines; rebuild adds spacing deterministically
        m = _CONF_TS_RE.search(ln)
        if not m:
            kept.append(ln); continue
        try:
            conf = float(m.group(1)); ts = m.group(2)
        except Exception:
            kept.append(ln); continue
        if ts == today:
            kept.append(ln); continue  # fresh this pass, don't decay
        new_conf = round(conf - DECAY_PER_PASS, 4)
        if new_conf < DECAY_FLOOR:
            # strip leading '- ', any prior [DECAYED ...] prefixes, and the conf tag
            core = _CONF_TS_RE.sub("", ln).strip().lstrip("- ").strip()
            core = _DECAYED_PREFIX_RE.sub("", core).strip()
            decayed_out.append(core)
            changed = True
            continue
        ln2 = _CONF_TS_RE.sub(f"(conf: {new_conf}, ts: {ts})", ln, count=1)
        kept.append(ln2)
        changed = True
    return kept, changed

def _parse_sections(txt):
    """Return (frontmatter_and_title, {SECTION: [nonblank body lines]}). Robust to
    duplicate/mangled headers: bodies for the same header name are concatenated."""
    parts = re.split(r"(^## .+$)", txt, flags=re.MULTILINE)
    head = parts[0]
    sections = {}
    i = 1
    while i < len(parts):
        name = parts[i].strip("# ").strip().upper()
        body = parts[i + 1] if i + 1 < len(parts) else ""
        lines = [l for l in body.splitlines() if l.strip()]
        sections.setdefault(name, []).extend(lines)
        i += 2
    return head, sections

def compact_rep(path):
    """Apply one decay pass to a single rep file, REBUILDING from canonical section
    order so structure can never drift. Fail-open; NEVER deletes the file."""
    try:
        txt = path.read_text(encoding="utf-8")
    except Exception as e:
        log(f"   ⚠️  compact read failed ({path.name}): {e}")
        return False
    today = datetime.utcnow().strftime("%Y-%m-%d")
    head, sections = _parse_sections(txt)
    decayed = []
    changed_any = False
    for name in ("FACTS", "BEAT"):
        body = "\n".join(sections.get(name, []))
        kept, ch = _decay_section(body, today, decayed)
        sections[name] = kept
        changed_any = changed_any or ch
    if decayed:
        prov = sections.setdefault("PROVENANCE", [])
        for d in decayed:
            prov.append(f"- [DECAYED {today}] {d}")
    if not (changed_any or decayed):
        return False
    # Rebuild deterministically: canonical order, one blank line after each header.
    out = [head.rstrip("\n") + "\n"]
    for name in _SECTION_ORDER:
        out.append(f"\n## {name}\n")
        for ln in sections.get(name, []):
            out.append(ln + "\n")
    # any unexpected extra sections (defensive) appended at end, once
    for name, lines in sections.items():
        if name not in _SECTION_ORDER:
            out.append(f"\n## {name}\n")
            for ln in lines:
                out.append(ln + "\n")
    txt2 = "".join(out)
    txt2 = re.sub(r"^last_updated:.*$", f"last_updated: {today}", txt2, count=1, flags=re.MULTILINE)
    try:
        path.write_text(txt2, encoding="utf-8")
        log(f"   🧹 compacted {path.name}: {len(decayed)} decayed-out, decay pass applied")
        return True
    except Exception as e:
        log(f"   ⚠️  compact write failed ({path.name}): {e}")
    return False

def run_compaction():
    """Sweep all peer reps with one decay pass. Never deletes a rep file."""
    if not PEERS_DIR.exists():
        log("✅ no peers dir — nothing to compact")
        return
    reps = sorted(PEERS_DIR.glob("*.md"))
    if not reps:
        log("✅ no peer reps — nothing to compact")
        return
    n = sum(1 for r in reps if compact_rep(r))
    log(f"✅ compaction pass: {n}/{len(reps)} rep(s) changed")

# ═══ MAIN ═══════════════════════════════════════════
def main():
    # --compact mode: run a decay sweep instead of archive derive.
    if "--compact" in sys.argv:
        run_compaction()
        return
    # Single-flight lock (mirror extract_memory) — fail-open if lock unavailable.
    lock_fh = None
    try:
        lock_fh = open(EXTRACT_LOCK_FILE, "w")
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except Exception:
        log("⚠️  another extract_user run holds the lock — exiting")
        return

    try:
        if not SESSIONS_DIR.exists():
            log(f"⚠️  sessions dir missing: {SESSIONS_DIR} — nothing to do")
            write_status(True, 0, "no sessions dir")
            return
        all_archives = [f.name for f in SESSIONS_DIR.glob("*.archived.*.jsonl")]
        processed = load_processed_set()
        new_archives = [f for f in all_archives if f not in processed]
        if not new_archives:
            log("✅ no new archives for peer derive")
            write_status(True, 0, "no backlog")
            return
        log(f"🔄 peer-derive {len(new_archives)} new archive(s), batch {BATCH_SIZE}")
        done = []
        for fn in new_archives[:BATCH_SIZE]:
            try:
                if process_archive(fn):
                    done.append(fn)
            except Exception as e:
                log(f"   ⚠️  {fn}: unhandled — fail-open mark processed: {e}")
                done.append(fn)  # fail-open: don't retry a poison archive forever
        processed.update(done)
        save_processed_set(processed)
        remaining = len(new_archives) - len(done)
        write_status(True, remaining, f"processed {len(done)}")
        log(f"✅ peer-derive done: {len(done)} processed, {remaining} remaining")
    finally:
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_UN)
            lock_fh.close()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # Absolute fail-open: extract_user must NEVER break the memory pipeline.
        try:
            log(f"💥 extract_user top-level error (fail-open): {e}")
            write_status(False, -1, f"top-level error: {e}")
        except Exception:
            pass
        sys.exit(0)
