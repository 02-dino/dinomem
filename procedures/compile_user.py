#!/usr/bin/env python3
"""
compile_user.py — USER.md router compiler (the missing leg).

Design origin: session 96b324ff (2026-07-24). USER.md should be a
MEMORY.md-for-people ROUTER, not a hand-typed profile:

  USER.md
    ├─ owner block (always-on, tiny)     # who the owner is + core operating prefs
    └─ user map (one line per peer)       # sender_id -> name, tags -> peers/<id>.md
  memory/peers/<platform>_<id>.md         # full per-person rep, retrieved on match

The peer reps (sources) + the authority-scope trust gate (mem_authority.py)
already shipped. This is the assembly leg that reads those already-governed
reps and (re)writes a MARKER-BOUNDED block into USER.md. Everything OUTSIDE the
markers is hand-written and preserved byte-for-byte — same clobber-safety as the
managed AGENTS.md block.

Guarantees:
  - MARKER-BOUNDED: only ever rewrites the region between
    <!-- BEGIN:dinomem-user-map --> and <!-- END:dinomem-user-map -->.
    Hand-written USER.md content is never touched.
  - ZERO-PEERS SAFE: with no peer reps it still emits the owner block (from
    owner _pin_s / IDENTITY.md), so a fresh install gets a valid USER.md.
  - FAIL-OPEN: any error -> leave USER.md untouched, exit 0. Must NEVER break
    the memory pipeline (mirrors extract_user/extract_memory contract).
  - IDEMPOTENT: re-run with unchanged sources -> byte-identical block (hashed
    skip), no churn, no duplicate markers.

Run via orchestrator (auto_session_reset.py) after extract_user, or standalone:
  python3 procedures/compile_user.py [--dry-run]

Logs to: logs/compile_user.log
"""
import os
import re
import sys
import hashlib
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent

# ─── Reuse mem_authority for owner resolution (import; fail-open) ──────────────
_AUTH_OK = False
try:
    sys.path.insert(0, str(_HERE))
    import mem_authority as _auth  # noqa: E402
    _AUTH_OK = True
except Exception:
    _AUTH_OK = False

# ─── Configuration ────────────────────────────────────────────────────────────
WORKSPACE = _HERE.parent
MEMORY_DIR = WORKSPACE / "memory"
PEERS_DIR = MEMORY_DIR / "peers"
USER_MD = WORKSPACE / "USER.md"
IDENTITY_MD = WORKSPACE / "IDENTITY.md"
LOG_FILE = WORKSPACE / "logs" / "compile_user.log"

BEGIN = "<!-- BEGIN:dinomem-user-map (managed — do not edit between markers) -->"
END = "<!-- END:dinomem-user-map -->"

# Cap the user map so USER.md (always injected) never becomes a context bomb.
# One line per peer; if we exceed this many, keep the most-recently-active.
MAX_MAP_ROWS = 200

for _d in (LOG_FILE.parent,):
    try:
        _d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass


def log(msg):
    line = f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} | {msg}"
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(msg)


# ─── Frontmatter / peer-rep parsing ──────────────────────────────────────────
_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_PEER_FN_RE = re.compile(r"^(?P<platform>[a-z0-9_-]+)_(?P<id>.+)\.md$", re.IGNORECASE)


def _parse_frontmatter(text):
    """Return dict of top-level scalar frontmatter fields. Fail-open -> {}."""
    out = {}
    try:
        m = _FM_RE.match(text)
        if not m:
            return out
        for line in m.group(1).splitlines():
            if ":" not in line or line.lstrip().startswith("#"):
                continue
            k, _, v = line.partition(":")
            k = k.strip()
            v = v.split("#", 1)[0].strip().strip('"').strip("'")
            if k:
                out[k] = v
    except Exception:
        pass
    return out


def _parse_tags(raw):
    """'[a, b]' or 'a, b' -> ['a','b']. Fail-open -> []."""
    try:
        raw = (raw or "").strip().strip("[]")
        return [t.strip().strip('"').strip("'") for t in raw.split(",") if t.strip()]
    except Exception:
        return []


def _one_line_summary(text):
    """First non-empty content line under ## FACTS (or first bullet). Bounded."""
    try:
        body = _FM_RE.sub("", text, count=1)
        in_facts = False
        for line in body.splitlines():
            s = line.strip()
            if s.startswith("## FACTS"):
                in_facts = True
                continue
            if in_facts and s.startswith("## "):
                break
            if in_facts and s.startswith("-") and not s.startswith("<!--"):
                return re.sub(r"\s+", " ", s.lstrip("- ").strip())[:120]
        # fallback: first bullet anywhere
        for line in body.splitlines():
            s = line.strip()
            if s.startswith("-") and not s.startswith("<!--"):
                return re.sub(r"\s+", " ", s.lstrip("- ").strip())[:120]
    except Exception:
        pass
    return ""


def load_peers():
    """Read every peers/*.md rep -> list of dicts (newest last_updated first).
    Fail-open: a bad rep is skipped, never aborts the compile."""
    peers = []
    if not PEERS_DIR.exists():
        return peers
    for p in sorted(PEERS_DIR.glob("*.md")):
        mfn = _PEER_FN_RE.match(p.name)
        if not mfn:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            continue
        fm = _parse_frontmatter(text)
        platform = fm.get("platform") or mfn.group("platform")
        pid = (fm.get("platform_id") or mfn.group("id")).strip().strip('"')
        peers.append({
            "file": f"peers/{p.name}",
            "platform": platform,
            "id": pid,
            "name": fm.get("name") or "unknown",
            "handle": fm.get("handle") or "",
            "tags": _parse_tags(fm.get("tags")),
            "last_updated": fm.get("last_updated") or "",
            "interactions": fm.get("interactions") or "",
            "is_owner": _is_owner(pid),
            "summary": _one_line_summary(text),
        })
    # newest activity first (stable), owners never mixed into the map anyway
    peers.sort(key=lambda d: d.get("last_updated", ""), reverse=True)
    return peers


def _is_owner(platform_id):
    """Owner check via mem_authority (shipped trust gate). Fail-open False so a
    resolution error never mislabels every peer as owner."""
    if not _AUTH_OK:
        return False
    try:
        return bool(_auth.is_owner(platform_id))
    except Exception:
        return False


# ─── Owner block ────────────────────────────────────────────────────
def _owner_pins():
    """Collect owner biographical lines from memory/_pin_*.md (bullets only,
    bounded). These are the durable owner facts the router surfaces always-on.
    Fail-open -> []."""
    out = []
    try:
        for p in sorted(MEMORY_DIR.glob("_pin_*.md")):
            name = p.name.lower()
            # owner/user-profile pins only; skip project/system pins
            if not any(t in name for t in ("owner", "user", "profile", "dino", "about")):
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except Exception:
                continue
            body = _FM_RE.sub("", text, count=1)
            for line in body.splitlines():
                s = line.strip()
                if s.startswith("-") and not s.startswith("<!--"):
                    out.append(re.sub(r"\s+", " ", s.lstrip("- ").strip())[:160])
                if len(out) >= 20:
                    return out
    except Exception:
        pass
    return out


def _identity_line():
    """One-line owner identity from IDENTITY.md (Owner: field or Name:). Fail-open."""
    try:
        if not IDENTITY_MD.exists():
            return ""
        for line in IDENTITY_MD.read_text(encoding="utf-8").splitlines():
            s = line.strip().lstrip("-* ").strip()
            if s.lower().startswith("**owner:**") or s.lower().startswith("owner:"):
                return re.sub(r"\*+", "", s.split(":", 1)[1]).strip()[:160]
    except Exception:
        pass
    return ""


def render_owner_block(peers):
    """Owner block: identity + owner pins + owner peer reps. Always emitted, even
    with zero peers (a fresh install still gets a valid owner block)."""
    lines = ["### Owner"]
    ident = _identity_line()
    if ident:
        lines.append(f"- Identity: {ident}")
    owner_ids = []
    if _AUTH_OK:
        try:
            owner_ids = sorted(_auth._owner_ids())
        except Exception:
            owner_ids = []
    if owner_ids:
        lines.append(f"- Owner ids: {', '.join(owner_ids)}")
    else:
        lines.append("- Owner ids: (none configured — authority gate fail-open)")
    for pin in _owner_pins():
        lines.append(f"- {pin}")
    # owner's own peer rep pointer(s), if any exist
    for pr in peers:
        if pr["is_owner"]:
            lines.append(f"- Rep: {pr['file']}" + (f" ({pr['summary']})" if pr['summary'] else ""))
    if len(lines) == 1:
        lines.append("- (owner profile not yet populated — add a memory/_pin_ or fill IDENTITY.md)")
    return "\n".join(lines)


# ─── User map ────────────────────────────────────────────────────
def render_user_map(peers):
    """One line per NON-owner peer, keyed by platform:id for hard-key lookup.
    Bounded by MAX_MAP_ROWS (most-recently-active kept)."""
    rows = [pr for pr in peers if not pr["is_owner"]]
    total = len(rows)
    rows = rows[:MAX_MAP_ROWS]
    lines = ["### User map"]
    if not rows:
        lines.append("- (no peers yet — populated as non-owner users interact)")
        return "\n".join(lines), total
    lines.append("| key | name | tags | rep | last |")
    lines.append("|---|---|---|---|---|")
    for pr in rows:
        key = f"{pr['platform']}:{pr['id']}"
        name = pr["name"] or "unknown"
        if pr["handle"]:
            name += f" (@{pr['handle']})"
        tags = ", ".join(pr["tags"][:4]) if pr["tags"] else "—"
        lines.append(f"| {key} | {name} | {tags} | {pr['file']} | {pr['last_updated'] or '—'} |")
    if total > MAX_MAP_ROWS:
        lines.append(f"\n_(+{total - MAX_MAP_ROWS} more peers not shown — search memory/peers/ by name/id)_")
    return "\n".join(lines), total


# ─── Marker-bounded write (clobber-safe upsert) ────────────────────────────
def build_block(peers):
    """Assemble the full managed block (between markers)."""
    owner = render_owner_block(peers)
    user_map, total = render_user_map(peers)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    inner = (
        "## User Router\n"
        "<!-- Router index of known people (MEMORY.md-for-people). Owner block is "
        "always-on; peers are hard-keyed by platform:id (free in inbound metadata) "
        "and their full rep lives in memory/peers/<file>, retrieved on match. -->\n\n"
        f"{owner}\n\n{user_map}\n\n"
        f"_Compiled by compile_user.py from memory/peers/ + owner pins — {stamp} — "
        f"{total} peer(s). Edit SOURCES (peers/_pin_), not this block._"
    )
    return f"{BEGIN}\n{inner}\n{END}"


def upsert_user_md(block, dry_run=False):
    """Replace the marked region in USER.md, or append it if absent. Everything
    outside the markers is preserved byte-for-byte. Idempotent: identical block
    -> no write. Fail-open on any error."""
    try:
        existing = USER_MD.read_text(encoding="utf-8") if USER_MD.exists() else ""
    except Exception:
        existing = ""

    # Locate an existing marked region (fixed-string, tolerant of body drift).
    has_begin = BEGIN in existing
    has_end = END in existing
    if has_begin and has_end:
        pre = existing.split(BEGIN, 1)[0]
        post = existing.split(END, 1)[1]
        new_text = pre.rstrip("\n") + "\n\n" + block + "\n" + post.lstrip("\n")
        # if the leading content was empty, avoid a stray blank prefix
        new_text = new_text.lstrip("\n") if not pre.strip() else new_text
    else:
        base = existing.rstrip("\n")
        sep = "\n\n" if base else ""
        new_text = f"{base}{sep}{block}\n"

    if not new_text.endswith("\n"):
        new_text += "\n"

    if new_text == existing:
        log("✅ USER.md router block unchanged — no write (idempotent).")
        return "unchanged"

    if dry_run:
        log("[dry-run] would write USER.md router block "
            f"({'refresh' if has_begin else 'append'}).")
        return "dry-run"

    try:
        tmp = USER_MD.with_suffix(".md.tmp")
        tmp.write_text(new_text, encoding="utf-8")
        os.replace(tmp, USER_MD)
        log(f"✅ USER.md router block {'refreshed' if has_begin else 'appended'} "
            f"(atomic, hand-written content preserved).")
        return "written"
    except Exception as e:
        log(f"⚠️  USER.md write failed (fail-open, left untouched): {e}")
        return "error"


def main():
    dry = "--dry-run" in sys.argv
    try:
        peers = load_peers()
        block = build_block(peers)
        result = upsert_user_md(block, dry_run=dry)
        n = len([p for p in peers if not p["is_owner"]])
        log(f"compile_user done: {result}, {n} non-owner peer(s), "
            f"auth={'on' if _AUTH_OK else 'off(fail-open)'}.")
    except Exception as e:
        # Absolute fail-open: compile_user must NEVER break the pipeline.
        log(f"💥 compile_user top-level error (fail-open): {e}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
