#!/usr/bin/env python3
"""
improvement_scan.py — L1 detector for dinomem auto procedural self-improvement.

WHY THIS EXISTS: dinomem's build stack (route.py -> leaf writers -> verify.sh)
is entirely MANUAL; it only fires when a human asks. Hermes closes that gap by
forking an LLM review after every N turns, blindly, whether or not the turn
contained anything worth learning. This is the cheaper half of the same idea:
a ZERO-LLM structural pass answering one narrow question — "WHICH archived
sessions plausibly contain a lesson?" — and handing those spans to the L2
proposer. Structure cannot understand meaning, so it is never asked to. It only
triages, so the expensive LLM reads ~7% of sessions instead of 100%.

MEASURED CONSTRAINT (2026-10-22, 971 analyst sessions / 13,597 messages):
archived .jsonl carries toolCall parts with ONLY {name, arguments} — there is
NO toolResult, no exit code, no stdout/stderr (18 stray lines corpus-wide).
"Detect an error, then detect the later success" is therefore IMPOSSIBLE on
this data. Every signal below is derived from CALL SHAPE alone.

THE THREE SIGNALS (calibrated on the real corpus, not invented):
  struggle   same tool repeated with NEARLY-but-not-exactly the same arguments.
             The naive form (same tool 3x, args differ at all) fired on 31% of
             sessions and was junk — consecutive `exec` is just normal work.
             Requiring argument SIMILARITY inside a band cut it to 7%, which
             reads as genuine "retry the same target with a tweak".
  multistep  a long run of VARIED tools in one session (workflow worth saving).
  interrupt  the user speaks MID-WORK and the tool set CHANGES afterwards. The
             second half is what lifts this above keyword matching: a
             correction the agent ignored taught it nothing.

OUTPUT: candidate JSON under <workspace>/kb/improvement_queue/, each pointing at
an evidence span (file + message indices). L2 reads the span itself; this tool
deliberately does not copy conversation text into the queue.

Also emits skill_usage.json (read calls against */skills/<name>/SKILL.md) which
the L4 curator consumes — so skill usage needs NO new instrumentation.

Usage:
  improvement_scan.py                 # scan new archives since watermark
  improvement_scan.py --dry-run       # report only; writes nothing, no watermark move
  improvement_scan.py --all           # ignore watermark, rescan everything
  improvement_scan.py --json          # machine-readable summary on stdout
  improvement_scan.py --limit N       # cap candidates emitted this run

Exit codes: 0 = ok (including "nothing to do"), 1 = sessions dir unreadable.
ZERO LLM. Read-only against sessions; writes only inside kb/improvement_queue/.
"""

import argparse
import difflib
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

# ── Tunables (constants ON PURPOSE: retune without touching logic) ───────────
# Argument-similarity band for `struggle`. Below LO the two calls are different
# work; above HI it is a verbatim repeat (a loop/glitch, not a lesson).
# Measured on 971 sessions: naive=297 (31%, junk), this band=69 (7%, sane).
STRUGGLE_SIM_LO = 0.55
STRUGGLE_SIM_HI = 0.97
STRUGGLE_RUN = 3            # consecutive near-repeats needed to flag

MULTISTEP_MIN_CALLS = 6     # total toolCalls in the session
MULTISTEP_MIN_DISTINCT = 3  # distinct tool names (guards one-tool-x20)

INTERRUPT_MIN_PRIOR_CALLS = 2  # user must speak AFTER real work started

# Owner ruling 2026-10-22: at most 5 SESSIONS reach L2 per run; the rest wait
# for the next run. NOT a token saving (~9k/day total, trivial) — a
# review-fatigue guard. An approval gate the owner stops reading is identical
# to having no gate at all. One candidate == one session (see _candidate_id),
# so this is both the session ceiling and the queue-spam ceiling.
MAX_SESSIONS_PER_RUN = 5
ARG_CLIP = 400                 # chars of argument JSON compared (perf + noise)

# Excerpt budget. MEASURED 2026-10-22: raw spans for 60 candidates totalled
# 9.48 MB (~2.37M tokens), median 44 KB, largest 1.93 MB for ONE candidate —
# bigger than a context window. So extraction is not an optimisation, it is what
# makes L2 possible at all. The excerpt keeps the SHAPE (who did what, in order)
# and drops the bulk (tool output pasted into text, long arguments).
EXCERPT_MAX_CHARS = 6000       # hard ceiling per candidate
EXCERPT_ARG_CLIP = 220         # per tool-call argument preview
EXCERPT_TEXT_CLIP = 500        # per user/assistant message preview
EXCERPT_HEAD = 6               # events kept from the start of the span
EXCERPT_TAIL = 10              # events kept from the end (the fix usually lands here)

# ── Workspace / sessions resolution ──────────────────────────────────────────
# Same ladder every dinomem procedure uses: env > install-time sed > self-locate.
# Self-locate keeps this working when install-time substitution was skipped,
# which is what makes ONE file valid across every installed agent.
_WS_DEFAULT = "DINOMEM_WORKSPACE_PLACEHOLDER"
if _WS_DEFAULT.startswith("DINOMEM_"):
    _WS_DEFAULT = str(Path(__file__).resolve().parent.parent)
WORKSPACE = Path(os.environ.get("DINOMEM_WORKSPACE", _WS_DEFAULT))

_SESS_DEFAULT = "DINOMEM_AGENT_SESSIONS_PLACEHOLDER"
if _SESS_DEFAULT.startswith("DINOMEM_"):
    _SESS_DEFAULT = ""
SESSIONS_DIR = Path(
    os.environ.get("DINOMEM_AGENT_SESSIONS") or _SESS_DEFAULT or "/nonexistent")

QUEUE_DIR = WORKSPACE / "kb" / "improvement_queue"
WATERMARK = QUEUE_DIR / ".watermark"
SKILL_USAGE = QUEUE_DIR / "skill_usage.json"

ARCHIVE_GLOB = "*.archived.*.jsonl"
_SKILL_RE = re.compile(r"skills/([A-Za-z0-9][A-Za-z0-9._-]*)/SKILL\.md")


# ── Parsing ──────────────────────────────────────────────────────────────────
def parse_session(path):
    """Flatten one archived .jsonl into an ordered event list + skill reads.

    Returns (events, skills) where events are tuples:
        ("U", idx)                  user message
        ("T", idx, tool_name, args) assistant tool call
    `idx` is the 0-based LINE index in the file, which is what makes an
    evidence span re-readable by L2 without copying any text here.

    Malformed lines are skipped, never fatal: these archives are written by a
    live gateway and a single truncated tail line must not blind the scan.
    """
    events, skills = [], []
    try:
        fh = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return events, skills
    with fh:
        for idx, line in enumerate(fh):
            if '"message"' not in line:
                continue
            try:
                obj = json.loads(line)
            except (ValueError, TypeError):
                continue
            msg = obj.get("message")
            if not isinstance(msg, dict):
                continue
            role, content = msg.get("role"), msg.get("content")
            if role == "user" and isinstance(content, str):
                events.append(("U", idx))
            elif isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict) or part.get("type") != "toolCall":
                        continue
                    name = part.get("name") or ""
                    try:
                        args = json.dumps(part.get("arguments"), ensure_ascii=False,
                                          sort_keys=True)[:ARG_CLIP]
                    except (TypeError, ValueError):
                        args = ""
                    events.append(("T", idx, name, args))
                    skills.extend(_SKILL_RE.findall(args))
    return events, skills


# ── Signals ──────────────────────────────────────────────────────────────────
# Each detector returns None (no signal) or a dict carrying the evidence span.
# They never read message TEXT — only call shape — so they cannot be fooled by,
# nor do they depend on, the language the conversation happens to be in.

def detect_struggle(events):
    """Same tool retried with a NEARLY identical argument = stuck on one target.

    The similarity band is the whole trick. Requiring merely "args differ" also
    matches ordinary sequential work (five unrelated `exec` calls) and fired on
    31% of sessions. Requiring near-identity instead isolates the retry shape.
    """
    calls = [e for e in events if e[0] == "T"]
    run, start = 1, None
    for i in range(len(calls) - 1):
        cur, nxt = calls[i], calls[i + 1]
        if cur[2] == nxt[2]:
            sim = difflib.SequenceMatcher(None, cur[3], nxt[3]).ratio()
            if STRUGGLE_SIM_LO <= sim <= STRUGGLE_SIM_HI:
                if run == 1:
                    start = cur
                run += 1
                if run >= STRUGGLE_RUN:
                    return {"signal": "struggle", "tool": cur[2],
                            "similarity": round(sim, 3), "repeats": run,
                            "line_start": start[1], "line_end": nxt[1]}
                continue
        run, start = 1, None
    return None


def detect_multistep(events):
    """A long run of VARIED tools = a workflow that may be worth saving.

    The distinct-tool floor matters: 20 calls to one tool is a loop, not a
    procedure, and saving it as a skill would teach nothing.
    """
    calls = [e for e in events if e[0] == "T"]
    names = {c[2] for c in calls}
    if len(calls) >= MULTISTEP_MIN_CALLS and len(names) >= MULTISTEP_MIN_DISTINCT:
        return {"signal": "multistep", "calls": len(calls),
                "distinct_tools": len(names), "tools": sorted(names)[:12],
                "line_start": calls[0][1], "line_end": calls[-1][1]}
    return None


def detect_interrupt(events):
    """User speaks MID-WORK and the tool set CHANGES afterwards.

    Both halves are required. A user message alone is just conversation; a
    correction the agent did not act on produced no lesson. The change in tools
    is the structural proxy for "the approach actually shifted".
    """
    prior_calls = 0
    seen = set()
    for i, ev in enumerate(events):
        if ev[0] == "T":
            prior_calls += 1
            seen.add(ev[2])
            continue
        if prior_calls < INTERRUPT_MIN_PRIOR_CALLS:
            continue
        after = [e for e in events[i + 1:] if e[0] == "T"]
        new = {e[2] for e in after} - seen
        if after and new:
            return {"signal": "interrupt", "user_line": ev[1],
                    "calls_before": prior_calls, "new_tools": sorted(new)[:8],
                    "line_start": ev[1], "line_end": after[-1][1]}
    return None


DETECTORS = (detect_struggle, detect_multistep, detect_interrupt)


# ── Excerpt ──────────────────────────────────────────────────────────────────
def build_excerpt(path, finding):
    """Render ONLY the evidence span as a compact, ordered transcript sketch.

    WHY (measured 2026-10-22): raw spans averaged 44 KB and peaked at 1.93 MB
    for a single candidate — larger than a context window. Handing L2 a span
    pointer alone would force it to re-read, re-parse and re-truncate the whole
    session, which is both the expensive path AND the one that fails outright on
    the big candidates. So L1 does the extraction once, deterministically.

    Head+tail, never the middle: the start shows what was attempted and the tail
    shows what finally worked (or what the user said). The middle of a long
    struggle is repetition by definition — that is what made it a candidate.
    """
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return [], {"truncated": False, "events_total": 0, "chars": 0}

    lo = max(0, int(finding.get("line_start", 0)))
    hi = min(len(lines) - 1, int(finding.get("line_end", lo)))

    events = []
    for raw in lines[lo:hi + 1]:
        if '"message"' not in raw:
            continue
        try:
            msg = (json.loads(raw) or {}).get("message")
        except (ValueError, TypeError):
            continue
        if not isinstance(msg, dict):
            continue
        role, content = msg.get("role"), msg.get("content")
        if isinstance(content, str):
            text = " ".join(content.split())
            if text:
                events.append({"role": role or "?",
                               "text": text[:EXCERPT_TEXT_CLIP]})
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "toolCall":
                    try:
                        args = json.dumps(part.get("arguments"),
                                          ensure_ascii=False, sort_keys=True)
                    except (TypeError, ValueError):
                        args = ""
                    events.append({"role": "tool", "tool": part.get("name") or "",
                                   "args": " ".join(args.split())[:EXCERPT_ARG_CLIP]})
                elif part.get("type") == "text":
                    text = " ".join(str(part.get("text") or "").split())
                    if text:
                        events.append({"role": role or "assistant",
                                       "text": text[:EXCERPT_TEXT_CLIP]})

    total = len(events)
    truncated = False
    if total > EXCERPT_HEAD + EXCERPT_TAIL:
        omitted = total - EXCERPT_HEAD - EXCERPT_TAIL
        events = (events[:EXCERPT_HEAD]
                  + [{"role": "_", "text": f"... {omitted} events omitted ..."}]
                  + events[-EXCERPT_TAIL:])
        truncated = True

    # Final byte ceiling: a single pathological argument/message must never blow
    # the budget even when the event count is already small.
    size = 0
    capped = []
    for ev in events:
        size += len(json.dumps(ev, ensure_ascii=False))
        if size > EXCERPT_MAX_CHARS:
            truncated = True
            break
        capped.append(ev)

    return capped, {"truncated": truncated, "events_total": total, "chars": size}


# ── Scan driver ─────────────────────────────────────────────────────────────
def _read_watermark():
    """Highest archive mtime already scanned. Missing/corrupt -> 0 (scan all)."""
    try:
        return float(WATERMARK.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0.0


def _candidate_id(path, signals):
    """Stable id from (file, signal set) so a re-scan of the same session
    overwrites rather than duplicating. This is the queue's dedup floor.

    Keyed on the SESSION, not on one signal: measured 2026-10-22, 275 findings
    came from only 138 sessions (~2 signals each). Per-signal ids would hand the
    owner two approval requests for one event — the fastest way to make a review
    gate get ignored, and an ignored gate is the same as no gate.
    """
    raw = f"{path.name}:{'+'.join(sorted(signals))}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def scan(since=0.0, limit=MAX_SESSIONS_PER_RUN):
    """Scan archives newer than `since`. Returns (candidates, skill_usage, stats).

    Cheapest-first by construction: the mtime filter runs before any file is
    opened, so the common case (no new sessions) costs one directory listing.
    """
    stats = {"files_seen": 0, "files_scanned": 0, "newest_mtime": since,
             "by_signal": {}}
    if not SESSIONS_DIR.is_dir():
        return [], {}, stats

    fresh = []
    for path in SESSIONS_DIR.glob(ARCHIVE_GLOB):
        stats["files_seen"] += 1
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime > since:
            fresh.append((mtime, path))
    fresh.sort()

    candidates, skill_usage = [], {}
    for mtime, path in fresh:
        events, skills = parse_session(path)
        stats["files_scanned"] += 1
        stats["newest_mtime"] = max(stats["newest_mtime"], mtime)
        for name in skills:
            skill_usage[name] = skill_usage.get(name, 0) + 1
        if len(candidates) >= limit:
            continue  # keep walking for skill usage; stop emitting candidates

        # Run every detector, then emit ONE candidate per SESSION. Measured
        # 2026-10-22: 275 findings came from 138 sessions, so per-finding
        # emission would ask the owner to approve the same event ~2x. A session
        # tripping several signals is stronger evidence, not several events.
        findings = [f for f in (d(events) for d in DETECTORS) if f]
        if not findings:
            continue
        for f in findings:
            stats["by_signal"][f["signal"]] = stats["by_signal"].get(f["signal"], 0) + 1

        # Union span: earliest evidence to latest, so one excerpt covers every
        # signal and L2 never needs a second read.
        span = {"line_start": min(f["line_start"] for f in findings),
                "line_end": max(f["line_end"] for f in findings)}
        excerpt, meta = build_excerpt(path, span)
        signals = [f["signal"] for f in findings]
        candidates.append({
            "id": _candidate_id(path, signals),
            "status": "new",
            "detected_at": int(time.time()),
            "signals": sorted(signals),
            "evidence": {
                # Span pointer is kept for AUDIT (so any proposal can be traced
                # back to the exact moment), but L2 reads `excerpt` and must
                # never open the session file: raw spans reach 1.9 MB, past a
                # context window.
                "session_file": str(path),
                "line_start": span["line_start"],
                "line_end": span["line_end"],
                "excerpt": excerpt,
                "excerpt_meta": meta,
            },
            "findings": findings,
        })
    return candidates, skill_usage, stats


def emit(candidates, skill_usage, stats):
    """Persist the queue + skill usage + watermark. Returns count written.

    Watermark advances ONLY here, so a --dry-run never consumes work and a
    crash mid-write leaves the next run re-scanning the same files (at worst a
    duplicate id, which the queue overwrites) rather than silently skipping.
    """
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    written = 0
    for cand in candidates:
        target = QUEUE_DIR / f"{cand['id']}.json"
        if target.exists():
            continue  # already queued (or already processed by L2)
        target.write_text(json.dumps(cand, ensure_ascii=False, indent=2),
                          encoding="utf-8")
        written += 1
    if skill_usage:
        merged = {}
        try:
            merged = json.loads(SKILL_USAGE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            merged = {}
        counts = merged.get("counts", {}) if isinstance(merged, dict) else {}
        for name, n in skill_usage.items():
            counts[name] = counts.get(name, 0) + n
        SKILL_USAGE.write_text(json.dumps(
            {"updated_at": int(time.time()), "counts": counts},
            ensure_ascii=False, indent=2), encoding="utf-8")
    WATERMARK.write_text(str(stats["newest_mtime"]), encoding="utf-8")
    return written


def main():
    ap = argparse.ArgumentParser(
        description="L1 zero-LLM detector: find sessions that plausibly contain a lesson")
    ap.add_argument("--dry-run", action="store_true",
                    help="report only; write nothing and do not advance the watermark")
    ap.add_argument("--all", action="store_true",
                    help="ignore the watermark and rescan every archive")
    ap.add_argument("--json", action="store_true", help="machine-readable summary")
    ap.add_argument("--limit", type=int, default=MAX_SESSIONS_PER_RUN,
                    help=f"max sessions emitted this run (default {MAX_SESSIONS_PER_RUN})")
    args = ap.parse_args()

    if not SESSIONS_DIR.is_dir():
        msg = (f"sessions dir not found: {SESSIONS_DIR} "
               "(set DINOMEM_AGENT_SESSIONS)")
        print(json.dumps({"ok": False, "error": msg}) if args.json else msg,
              file=sys.stderr)
        return 1

    since = 0.0 if args.all else _read_watermark()

    # FIRST RUN = SEED ONLY (now-and-forward). On a fresh install the archive
    # already holds up to 21 days of history, and proposing from it is worse
    # than proposing nothing: the owner cannot VERIFY a lesson drawn from a
    # session they no longer remember, and an unverifiable proposal is exactly
    # what trains someone to stop reading the gate. So the first run records
    # the watermark and emits nothing; day two onward sees only fresh sessions.
    # Backlog stays reachable, but only on an explicit --all.
    seeded = False
    if not args.all and not WATERMARK.exists():
        newest = 0.0
        if SESSIONS_DIR.is_dir():
            for path in SESSIONS_DIR.glob(ARCHIVE_GLOB):
                try:
                    newest = max(newest, path.stat().st_mtime)
                except OSError:
                    continue
        if not args.dry_run:
            QUEUE_DIR.mkdir(parents=True, exist_ok=True)
            WATERMARK.write_text(str(newest), encoding="utf-8")
        seeded = True
        msg = ("first run: watermark seeded to now; existing history skipped "
               "(use --all to scan the backlog)")
        print(json.dumps({"ok": True, "seeded": True, "candidates": 0,
                          "watermark": newest, "reason": msg})
              if args.json else msg)
        return 0

    candidates, skill_usage, stats = scan(since=since, limit=args.limit)

    # Cheapest exit: nothing new since the watermark. This is the 364-days-a-year
    # path and it must cost one directory listing, never a file read.
    if not stats["files_scanned"]:
        out = {"ok": True, "scanned": 0, "candidates": 0, "reason": "no new sessions"}
        print(json.dumps(out) if args.json else "no new sessions since watermark")
        return 0

    written = 0 if args.dry_run else emit(candidates, skill_usage, stats)

    if args.json:
        print(json.dumps({"ok": True, "scanned": stats["files_scanned"],
                          "candidates": len(candidates), "written": written,
                          "by_signal": stats["by_signal"],
                          "dry_run": args.dry_run,
                          "items": candidates}, ensure_ascii=False, indent=2))
    else:
        mode = " (dry-run, nothing written)" if args.dry_run else ""
        print(f"scanned {stats['files_scanned']}/{stats['files_seen']} archives{mode}")
        print(f"candidates: {len(candidates)}  by signal: {stats['by_signal']}")
        for cand in candidates[:12]:
            ev = cand["evidence"]
            sigs = "+".join(cand["signals"])
            trunc = "~" if ev["excerpt_meta"]["truncated"] else " "
            print(f"  [{cand['id']}] {sigs:28s} "
                  f"{Path(ev['session_file']).name[:40]} "
                  f"L{ev['line_start']}-{ev['line_end']} "
                  f"{trunc}{ev['excerpt_meta']['chars']}B")
        if not args.dry_run:
            print(f"written: {written} new (queue: {QUEUE_DIR})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
