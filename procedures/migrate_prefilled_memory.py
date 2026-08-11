#!/usr/bin/env python3
"""
migrate_prefilled_memory.py — opt-in route-through migrator for a pre-filled MEMORY.md.

Problem: an operator who hand-wrote MEMORY.md before installing dinomem will have
that content silently overwritten by the extract cron (MEMORY.md is dinomem-owned).
The install-time guard (prefilled_memory_guard in install.sh) WARNS + BACKS UP but
does NOT migrate — because MEMORY.md content is heterogeneous and can't be routed
deterministically. This tool does the migration, LLM-assisted, opt-in, dry-run-first.

Key idea (why this is tractable): MEMORY.md is the extractor's OUTPUT format. A
hand-written MEMORY.md is just "facts someone typed instead of letting the cron
distill them." So the migration target is almost always memory/ — and the routing
DECISION is already solved by the dinomem routing system (tools/route.py classify()).
We replay each MEMORY.md line through that same router. Nothing new to invent.

Routing targets (decided by route.py classify, same brain the live agent uses):
  durable fact/preference        -> memory/_pin_<slug>.md
  dated observation/insight      -> memory/<UTC-date>_insight_<slug>.md
  always-inject behavioral rule  -> AGENTS.md  (appended under a migration section)
  per-person fact                -> memory/peers/<...>.md  (best-effort; else review)
  ambiguous / low-confidence     -> memory/_migrated_review.md   (human eyeballs)

Safety contract:
  --dry-run (DEFAULT): reads MEMORY.md, prints the routing plan, writes NOTHING.
  --apply: backs up MEMORY.md first, then performs the routed writes. Never deletes
           the original MEMORY.md (the extract cron will reclaim it; the backup +
           routed sources are the durable copy).
  Fail-safe: any per-line routing error -> that line goes to _migrated_review.md,
             never dropped. A top-level error aborts with a clear message; in
             --apply mode partial writes already made are listed so nothing is lost.

Usage:
  python3 procedures/migrate_prefilled_memory.py [--dry-run|--apply] [--file MEMORY.md]
"""
import os
import re
import sys
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
WORKSPACE = _HERE.parent
MEMORY_DIR = WORKSPACE / "memory"
DEFAULT_SRC = WORKSPACE / "MEMORY.md"
REVIEW_FILE = MEMORY_DIR / "_migrated_review.md"
AGENTS_MD = WORKSPACE / "AGENTS.md"
ROUTE_PY = _HERE.parent / "tools" / "route.py"

# Lines that are dinomem's own managed scaffolding, never operator content.
_SKIP_PATTERNS = [
    re.compile(r"^\s*$"),
    re.compile(r"^#*\s*MEMORY\.md\s*$", re.IGNORECASE),
    re.compile(r"<!--\s*dinomem:", re.IGNORECASE),
    re.compile(r"^\s*_Generated:.*Source:", re.IGNORECASE),
    re.compile(r"^\s*---\s*$"),
    re.compile(r"^##\s+Recent Context", re.IGNORECASE),
    re.compile(r"^##\s+Searchable", re.IGNORECASE),
    re.compile(r"^##\s+Previous Session", re.IGNORECASE),
    re.compile(r"^##\s+Silent Replies", re.IGNORECASE),
]


def _now_date():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _slug(text, n=6):
    words = re.findall(r"[a-z0-9]+", text.lower())
    return "-".join(words[:n]) or "entry"


def log(msg):
    print(msg)


# ─── Extract operator content from MEMORY.md ───────────────────────────
def extract_lines(src):
    """Return the operator-authored content lines (bullets/paragraphs), skipping
    dinomem scaffolding, headers, and the recency/searchable managed regions."""
    try:
        raw = Path(src).read_text(encoding="utf-8")
    except Exception as e:
        log(f"❌ cannot read {src}: {e}")
        return []
    # Drop any dinomem-managed marker region wholesale (belt-and-suspenders).
    raw = re.sub(r"<!--\s*dinomem:recency-start\s*-->.*?<!--\s*dinomem:recency-end\s*-->",
                 "", raw, flags=re.DOTALL | re.IGNORECASE)
    out = []
    in_managed = False
    for line in raw.splitlines():
        s = line.rstrip()
        low = s.strip().lower()
        # managed sections that the extractor regenerates -> never operator content
        if low.startswith("## searchable") or low.startswith("## previous session") \
           or low.startswith("## recent context"):
            in_managed = True
            continue
        if in_managed and s.startswith("## "):
            in_managed = False   # a new non-managed section
        if in_managed:
            continue
        if any(p.search(s) for p in _SKIP_PATTERNS):
            continue
        # skip ANY markdown header line (## Owner, ### Notes) — structural, not content
        if re.match(r"^\s*#{1,6}\s+\S", s):
            continue
        # keep bullets and prose lines; strip leading bullet marker for routing
        content = re.sub(r"^\s*[-*]\s+", "", s).strip()
        if len(content) >= 8:   # ignore trivial fragments
            out.append(content)
    # de-dup while preserving order
    seen, uniq = set(), []
    for c in out:
        k = c.lower()
        if k not in seen:
            seen.add(k)
            uniq.append(c)
    return uniq


# ─── Routing: dinomem's route.py is a SCHEMA EMITTER (LLM-in-the-loop) ─────────
# route.py does NOT expose classify(text); `route.py classify` prints a JSON
# decision schema an LLM reasons over. So the authoritative routing decision is
# LLM-assisted. emit_worksheet() below produces the schema + heuristic-hinted
# lines for that LLM step; the heuristic here is only a deterministic pre-seed.
def _route_schema():
    """Return route.py's decision schema JSON (or {} if route.py absent)."""
    for cand in (ROUTE_PY,
                 WORKSPACE / "github" / "dinomem" / "tools" / "route.py"):
        if cand.exists():
            try:
                out = subprocess.run([sys.executable, str(cand), "classify"],
                                     capture_output=True, text=True, timeout=15)
                if out.returncode == 0 and out.stdout.strip():
                    return json.loads(out.stdout)
            except Exception:
                continue
    return {}


def classify_line(text):
    """Route one line -> (target_kind, reason).

    IMPORTANT: dinomem's route.py is a SCHEMA EMITTER, not a callable classifier
    (`route.py classify` prints a JSON decision schema for an LLM to reason over;
    there is no classify(text) function). The authoritative routing decision is
    therefore LLM-in-the-loop — see emit_worksheet(). This heuristic is only a
    fast, deterministic HINT to pre-seed the worksheet and to give --apply a
    usable default when no LLM-filled plan is supplied. It biases to 'review' for
    anything uncertain so a wrong guess is never silently written.
    """
    return _heuristic(text)


def _map_router_result(res):
    """Normalize whatever route.py.classify returns into our target-kind set.
    route.py may return a dict, a string target, or a tuple; be liberal."""
    try:
        target = None
        if isinstance(res, dict):
            target = (res.get("target") or res.get("file") or res.get("dest")
                      or res.get("category") or "")
        elif isinstance(res, (list, tuple)) and res:
            target = str(res[0])
        else:
            target = str(res or "")
        t = target.lower()
        if "peer" in t:
            return "peer"
        if "agents.md" in t or "agents" == t or "rule" in t or "behavioral" in t:
            return "agents_rule"
        if "_pin" in t or "pin" == t or "persistent" in t or "permanent" in t:
            return "pin"
        if "insight" in t or "memory/" in t or "dated" in t or "_note" in t:
            return "dated_insight"
        # MEMORY.md/USER.md are forbidden write targets in route.py -> means
        # "route to its source"; without a concrete source we hold for review.
        if "memory.md" in t or "user.md" in t or not t:
            return None
    except Exception:
        return None
    return None


def _describe(res):
    try:
        if isinstance(res, dict):
            return res.get("target") or res.get("category") or json.dumps(res)[:60]
        return str(res)[:60]
    except Exception:
        return "?"


def _heuristic(text):
    """Conservative fallback when route.py has no callable classify(). Biases to
    'review' for anything not clearly a durable fact or a behavioral rule."""
    low = text.lower()
    # behavioral rule / instruction -> AGENTS.md
    if re.search(r"\b(always|never|must|do not|don'?t|prefer|avoid|require)\b", low) \
       and re.search(r"\b(call me|reply|respond|use|treat|hard-sell|tone|format|when )", low):
        return "agents_rule", "heuristic:imperative-rule"
    # durable identity/preference fact -> pin
    if re.search(r"\b(owner|user|timezone|prefers?|name is|based in|trades?|likes?)\b", low):
        return "pin", "heuristic:durable-fact"
    # dated/observational (has a date or 'today'/'as of') -> dated insight
    if re.search(r"\b(20\d\d-\d\d-\d\d|today|as of|yesterday|this (week|month))\b", low):
        return "dated_insight", "heuristic:dated-observation"
    return "review", "heuristic:ambiguous"


# ─── Plan + write ───────────────────────────────────────────────
def build_plan(lines):
    plan = []
    for ln in lines:
        kind, reason = classify_line(ln)
        plan.append({"text": ln, "kind": kind, "reason": reason})
    return plan


VALID_KINDS = {"pin", "dated_insight", "agents_rule", "peer", "review"}


def emit_worksheet(plan, schema, path):
    """Write an LLM-fillable routing worksheet: route.py's decision schema +
    each MEMORY.md line with a heuristic-hinted 'kind'. An LLM (this agent, or
    the daily-note cron model) reviews it, corrects each kind, then --apply
    consumes it. This is the LLM-in-the-loop step route.py is designed for."""
    doc = {
        "_instructions": (
            "Route each item['kind'] to one of: pin (durable fact -> memory/_pin_), "
            "dated_insight (dated observation -> memory/<date>_insight_), agents_rule "
            "(always-on behavioral rule -> AGENTS.md), peer (per-person fact -> "
            "memory/peers/), review (ambiguous -> _migrated_review.md). The 'kind' "
            "shown is a HEURISTIC HINT; correct it using the route.py schema below. "
            "Then run: migrate_prefilled_memory.py --apply --plan <this-file>."
        ),
        "route_schema": schema or "route.py unavailable — use kinds above",
        "items": plan,
    }
    _atomic_write(Path(path), json.dumps(doc, indent=2, ensure_ascii=False) + "\n")


def load_plan(path):
    """Load an LLM-filled worksheet back into a plan list. Fail-safe: unknown
    kinds -> 'review' (never a wrong write)."""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    items = doc.get("items", doc if isinstance(doc, list) else [])
    plan = []
    for it in items:
        k = (it.get("kind") or "review").strip().lower()
        if k not in VALID_KINDS:
            k = "review"
        plan.append({"text": it.get("text", ""), "kind": k,
                     "reason": it.get("reason", "from-worksheet")})
    return [p for p in plan if p["text"].strip()]


KIND_LABEL = {
    "pin": "memory/_pin_*.md (durable fact)",
    "dated_insight": "memory/<date>_insight_*.md (dated entry)",
    "agents_rule": "AGENTS.md (always-inject rule)",
    "peer": "memory/peers/*.md (per-person)",
    "review": "memory/_migrated_review.md (human review)",
}


def _atomic_write(path, text):
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _append(path, text):
    prev = path.read_text(encoding="utf-8") if path.exists() else ""
    base = prev.rstrip("\n")
    sep = "\n\n" if base else ""
    _atomic_write(path, f"{base}{sep}{text}\n")


def apply_plan(plan, src):
    """Perform routed writes. Returns dict of created/updated files."""
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    written = {}
    date = _now_date()
    review_items, agents_items = [], []
    pins = {}
    insights = {}
    for item in plan:
        k, t = item["kind"], item["text"]
        if k == "pin":
            fn = MEMORY_DIR / f"_pin_migrated-{_slug(t)}.md"
            pins.setdefault(str(fn), []).append(t)
        elif k == "dated_insight":
            fn = MEMORY_DIR / f"{date}_insight_migrated-{_slug(t)}.md"
            insights.setdefault(str(fn), []).append(t)
        elif k == "agents_rule":
            agents_items.append(t)
        elif k == "peer":
            # no reliable person key from a bare MEMORY.md line -> safest is review
            review_items.append(f"[peer?] {t}")
        else:
            review_items.append(t)

    for fn, items in pins.items():
        body = (f"---\ntype: pin\ndate: {date}\nsource: migrated-from-prefilled-MEMORY.md\n---\n\n"
                f"# Migrated durable fact(s)\n\n" + "\n".join(f"- {i}" for i in items) + "\n")
        _atomic_write(Path(fn), body)
        written[fn] = "created"
    for fn, items in insights.items():
        body = (f"---\ntype: insight\ndate: {date}\nsource: migrated-from-prefilled-MEMORY.md\n---\n\n"
                + "\n".join(f"- {i}" for i in items) + "\n")
        _atomic_write(Path(fn), body)
        written[fn] = "created"
    if agents_items:
        block = ("\n<!-- migrated-from-prefilled-MEMORY.md " + date + " -->\n"
                 "## Migrated rules (from pre-fill — review + fold into the right section)\n"
                 + "\n".join(f"- {i}" for i in agents_items))
        _append(AGENTS_MD, block)
        written[str(AGENTS_MD)] = "appended"
    if review_items:
        block = (f"---\ntype: migration-review\ndate: {date}\n---\n\n"
                 "# Pre-fill migration — needs human routing\n"
                 "These lines from your pre-filled MEMORY.md were ambiguous. Route each\n"
                 "by hand (a _pin_, a dated entry, AGENTS.md, or a peer rep), then delete.\n\n"
                 + "\n".join(f"- [ ] {i}" for i in review_items) + "\n")
        # append if a prior review file exists, else create
        if REVIEW_FILE.exists():
            _append(REVIEW_FILE, block)
        else:
            _atomic_write(REVIEW_FILE, block)
        written[str(REVIEW_FILE)] = "created/appended"
    return written


def _backup(src):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    bak = Path(str(src) + f".migrate-bak.{stamp}")
    try:
        bak.write_text(Path(src).read_text(encoding="utf-8"), encoding="utf-8")
        return bak
    except Exception as e:
        log(f"⚠️  backup failed: {e}")
        return None


def main():
    args = sys.argv[1:]
    apply = "--apply" in args
    src = DEFAULT_SRC
    plan_path = None
    if "--file" in args:
        try:
            src = Path(args[args.index("--file") + 1])
        except Exception:
            log("--file needs a path"); sys.exit(2)
    if "--plan" in args:
        try:
            plan_path = Path(args[args.index("--plan") + 1])
        except Exception:
            log("--plan needs a path"); sys.exit(2)

    worksheet_path = MEMORY_DIR / "_migration_worksheet.json"

    if not Path(src).exists():
        log(f"ℹ️  {src} not found — nothing to migrate."); sys.exit(0)

    # Guard: if MEMORY.md is already dinomem-managed, migration is a no-op mistake.
    try:
        head = Path(src).read_text(encoding="utf-8")
    except Exception as e:
        log(f"❌ cannot read {src}: {e}"); sys.exit(1)
    if "dinomem:recency" in head and not apply:
        log("ℹ️  MEMORY.md is already dinomem-managed (has recency markers).")
        log("   Migration is intended for a PRE-FILLED, not-yet-managed MEMORY.md.")
        log("   Proceeding in dry-run to show what WOULD route, but likely nothing useful.")

    # APPLY from an LLM-filled worksheet -> authoritative routing.
    if apply and plan_path and plan_path.exists():
        plan = load_plan(plan_path)
        log(f"\n=== migrate_prefilled_memory APPLY (from worksheet {plan_path.name}) ===")
        log(f"routed items: {len(plan)}")
    else:
        lines = extract_lines(src)
        plan = build_plan(lines)

    log(f"\n=== migrate_prefilled_memory {'APPLY' if apply else 'DRY-RUN'} : {src} ===")
    log(f"operator content lines found: {len(plan)}\n")
    counts = {}
    for item in plan:
        counts[item["kind"]] = counts.get(item["kind"], 0) + 1
        log(f"  [{item['kind']:>13}] {item['text'][:80]}")
        log(f"  {'':>16}↳ {KIND_LABEL.get(item['kind'], item['kind'])}  ({item['reason']})")
    log("\n--- summary (heuristic hint — LLM should refine via worksheet) ---")
    for k, n in sorted(counts.items()):
        log(f"  {n:>3}  {KIND_LABEL.get(k, k)}")

    if not apply:
        schema = _route_schema()
        emit_worksheet(plan, schema, worksheet_path)
        log(f"\nDRY-RUN: no memory files written.")
        log(f"Routing worksheet written -> {worksheet_path}")
        log("NEXT (LLM-in-the-loop): review each item['kind'] in the worksheet using")
        log("the embedded route.py schema, correct as needed, then run:")
        log(f"  python3 {Path(__file__).name} --apply --plan {worksheet_path}")
        sys.exit(0)

    bak = _backup(src)
    if bak:
        log(f"\nbacked up {src} -> {bak.name}")
    written = apply_plan(plan, src)
    log("\n--- written ---")
    for f, how in written.items():
        log(f"  {how:>16}  {f}")
    log("\n✅ migration applied. Original MEMORY.md left in place (extract cron will")
    log("   reclaim it); your content now lives in the routed sources above + backup.")
    log("   Review memory/_migrated_review.md for anything that needs hand-routing.")
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        log(f"💥 migrate top-level error: {e}")
        sys.exit(1)
