#!/usr/bin/env python3
"""stall_watch.py — zero-LLM detector for DEAD spawned subagents (the "stall").

WHY (incident 2026-11-15): workers spawned via sessions_spawn died (gateway
restart, idle timeout) and nothing noticed for ~6h. The note-side Advancer gate
(check_projects.sh) cannot tell a dead worker's FROZEN note from a note paused
on purpose, and 3 of the deaths had no note at all. So the SESSION store is the
primary trigger; the note is only the progress record used for the restart.

SOURCE: <agent sessions dir>/sessions.json, keys containing ':subagent:'.
COUNTING (owner ruling 2026-11-16 05:18):
  failed|timeout -> candidate; done -> never;
  running -> candidate only when silent > 2x runTimeoutSeconds ("ghost": the
  store said running forever after a gateway restart killed it). The status
  field alone LIES, so silence = max(lastActivityAt, sessionFile mtime).
SUPPRESSED (= already resolved, never fires) when any of:
  (a) same session continued after it ended (activity/mtime > end+60s)
  (b) a NEWER sibling for the same job is done, or running and alive
  (c) a linked note is status done, or was modified after the end
  (d) the parent spawned another subagent after the end
  (e) still inside the grace window (default 40 min) -> re-checked next tick
A fired stall is written to the ledger so it fires ONCE; the same job dying
again later has a new endedAt and is a new stall. The ledger also counts
restarts per job per day (the rescuer gives up at STALL_MAX_RESTARTS).

Stdlib only, no network, fast. Never kills or edits a session; the only file
it writes is its own ledger.

  stall_watch.py gate     [--dry-run]   JSON list of NEW stalls; exit 0 any, 1 none
  stall_watch.py pending                fired-but-unresolved stalls (+ session tail)
  stall_watch.py resolve  --key K --ended MS --action restarted|gaveup|skipped [--job J]
  stall_watch.py selftest               synthetic fixtures, exit 0 = all pass
"""
import argparse, json, os, re, sys, tempfile, time

_SESSIONS_BAKED = "DINOMEM_AGENT_SESSIONS_PLACEHOLDER"   # install.sh _apply_subst
_AGENT_BAKED = "DINOMEM_AGENT_ID_PLACEHOLDER"
NOTE_RE = re.compile(r"memory/_note_[A-Za-z0-9_.-]+\.md")
RESUME_SLACK_MS = 60_000
DEFAULT_RUN_TIMEOUT_S = 1800


def _env_int(name, default):
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _baked(v, env):
    """env wins; else the installer-substituted value; else None (unsubstituted)."""
    e = os.environ.get(env, "").strip()
    if e:
        return e
    return None if v.startswith("DINOMEM_") else v


def _paths():
    ws = os.environ.get("OPENCLAW_WORKSPACE") or os.path.expanduser("~/.openclaw/workspace")
    sess = _baked(_SESSIONS_BAKED, "DINOMEM_AGENT_SESSIONS")
    agent = _baked(_AGENT_BAKED, "DINOMEM_AGENT_ID") or ""
    if not sess and agent:
        sess = os.path.expanduser(f"~/.openclaw/agents/{agent}/sessions")
    ledger = os.environ.get("STALL_LEDGER") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), ".stall_watch.ledger.json")
    return ws, sess, agent, ledger


def run_timeout_s(agent):
    """subagents.runTimeoutSeconds of THIS gateway (per-agent override, then
    defaults). Each gateway has its own config home, so read the one the gateway
    exported (OPENCLAW_CONFIG_PATH / OPENCLAW_STATE_DIR) — never hardcode."""
    forced = _env_int("STALL_RUN_TIMEOUT_S", 0)
    if forced:
        return forced, "env"
    cands = [os.environ.get("OPENCLAW_CONFIG_PATH", "")]
    if os.environ.get("OPENCLAW_STATE_DIR"):
        cands.append(os.path.join(os.environ["OPENCLAW_STATE_DIR"], "openclaw.json"))
    cands.append(os.path.expanduser("~/.openclaw/openclaw.json"))
    for p in [c for c in cands if c]:
        try:
            ag = json.load(open(p)).get("agents", {})
        except Exception:
            continue
        for a in ag.get("list", []) or []:
            if a.get("id") == agent and (a.get("subagents") or {}).get("runTimeoutSeconds"):
                return int(a["subagents"]["runTimeoutSeconds"]), p
        v = (ag.get("defaults", {}).get("subagents") or {}).get("runTimeoutSeconds")
        if v:
            return int(v), p
    return DEFAULT_RUN_TIMEOUT_S, "fallback"


# ── session + note readers (all fail-soft: an unreadable file is "no evidence") ──
def _mtime_ms(p):
    try:
        return int(os.path.getmtime(p) * 1000) if p else None
    except OSError:
        return None


def _msg_of(line):
    """(role, text) of one transcript jsonl line; (None, '') if not a message."""
    try:
        o = json.loads(line)
    except ValueError:
        return None, ""
    m = o.get("message") or {}
    if o.get("type") != "message" or not isinstance(m, dict):
        return None, ""
    c = m.get("content")
    if isinstance(c, list):
        c = " ".join((x.get("text") or x.get("name") or "") for x in c if isinstance(x, dict))
    return m.get("role"), str(c or "")


def _first_user_text(path, max_lines=40):
    """The spawn brief = first user message of the subagent transcript. Only the
    head is read (transcripts reach MBs; a full read is what killed workers)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= max_lines:
                    break
                role, text = _msg_of(line)
                if role == "user":
                    return text
    except OSError:
        pass
    return ""


def _tail_text(path, nbytes=24_000, keep=6):
    """Last few message texts, so the rescuer sees where a note-less worker stopped."""
    out = []
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - nbytes))
            lines = f.read().decode("utf-8", "replace").splitlines()[1:]
    except OSError:
        return out
    for line in lines:
        role, text = _msg_of(line)
        if role and text:
            out.append(f"{role}: {text[:300]}")
    return out[-keep:]


def _frontmatter(path):
    fm = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            if f.readline().strip() != "---":
                return fm
            for line in f:
                if line.strip() == "---":
                    break
                k, sep, v = line.partition(":")
                if sep and not line.startswith((" ", "\t")):
                    fm[k.strip()] = v.strip()
    except OSError:
        pass
    return fm


def load_notes(ws):
    """{abs_path: {status, mtime, worker}} for every memory/_note_*.md."""
    notes, mdir = {}, os.path.join(ws, "memory")
    try:
        names = os.listdir(mdir)
    except OSError:
        return notes
    for n in names:
        if n.startswith("_note_") and n.endswith(".md"):
            p = os.path.join(mdir, n)
            fm = _frontmatter(p)
            notes[p] = {"status": fm.get("status", ""), "worker": fm.get("worker", ""),
                        "mtime": _mtime_ms(p)}
    return notes


def norm_label(label):
    """'logs sync (retry 2)' == 'Logs sync retry' == 'logs sync'. Retries are
    usually labelled by suffixing the original, so strip suffixes, not words."""
    s = (label or "").lower()
    s = re.sub(r"\([^)]*\)", " ", s)
    s = re.sub(r"\b(retry|resume|rerun|restart|again)\b.*$", " ", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def load_subagents(sess_dir, ws):
    store = json.load(open(os.path.join(sess_dir, "sessions.json")))
    notes = load_notes(ws)
    by_worker = {}
    for p, n in notes.items():
        for tok in re.findall(r"agent:[^\s()]+:subagent:[0-9a-f-]+", n["worker"]):
            by_worker.setdefault(tok, p)
    subs = []
    for key, v in store.items():
        if ":subagent:" not in key or not isinstance(v, dict):
            continue
        sf = v.get("sessionFile") or ""
        brief = _first_user_text(sf) if sf else ""
        if key in by_worker:
            linked = [by_worker[key]]
        else:
            # FIRST note named in the brief only: briefs lead with the worker's own
            # note and mention umbrella notes later as context; an umbrella note is
            # edited by everyone, so linking it would let (c) suppress real stalls.
            hits = NOTE_RE.findall(brief)
            linked = [os.path.join(ws, hits[0])] if hits else []
        m = re.search(r"STALL_JOB:\s*(\S+)", brief)
        job = m.group(1) if m else (os.path.basename(linked[0]) if linked else
                                    f"label:{norm_label(v.get('label'))}@{v.get('spawnedBy', '')}")
        subs.append({
            "key": key, "status": v.get("status") or "", "label": v.get("label") or "",
            "spawnedBy": v.get("spawnedBy") or "", "startedAt": v.get("startedAt") or 0,
            "endedAt": v.get("endedAt"), "lastActivityAt": v.get("lastActivityAt"),
            "sessionFile": sf, "file_mtime": _mtime_ms(sf), "notes": linked, "job": job,
            "norm": norm_label(v.get("label")),
        })
    return subs, notes


# ── classification ───────────────────────────────────────────────────────────
def last_seen(s):
    return max([x for x in (s["lastActivityAt"], s["file_mtime"]) if x] or [s["startedAt"] or 0])


def is_candidate(s, now, timeout_ms):
    """-> (candidate?, effective_end_ms, kind). done never counts."""
    st = s["status"]
    if st in ("failed", "timeout"):
        return True, s["endedAt"] or last_seen(s), st
    if st == "running" and now - last_seen(s) > 2 * timeout_ms:
        return True, last_seen(s), "ghost"
    return False, None, st


def _alive(s, now, timeout_ms):
    return s["status"] == "done" or (s["status"] == "running" and now - last_seen(s) <= 2 * timeout_ms)


def _same_job(a, b):
    if a["job"] == b["job"] or set(a["notes"]) & set(b["notes"]):
        return True
    return bool(a["norm"]) and a["norm"] == b["norm"] and a["spawnedBy"] == b["spawnedBy"]


def suppress_reason(s, end, subs, notes, now, timeout_ms, grace_ms):
    """First matching resolution rule, or None = genuine stall."""
    if s["status"] == "failed" or s["status"] == "timeout":
        if (s["lastActivityAt"] or 0) > end + RESUME_SLACK_MS or (s["file_mtime"] or 0) > end + RESUME_SLACK_MS:
            return "a:same-session-resumed"
    for o in subs:
        if o is s or o["startedAt"] <= s["startedAt"]:
            continue
        if _same_job(s, o) and _alive(o, now, timeout_ms):
            return f"b:newer-sibling-{o['status']}:{o['key'][-12:]}"
    for p in s["notes"]:
        n = notes.get(p)
        if n and n["status"] == "done":
            return f"c:note-done:{os.path.basename(p)}"
        if n and (n["mtime"] or 0) > end + RESUME_SLACK_MS:
            return f"c:note-moved:{os.path.basename(p)}"
    for o in subs:
        if o is not s and o["spawnedBy"] == s["spawnedBy"] and o["startedAt"] > end:
            return f"d:parent-spawned-after:{o['key'][-12:]}"
    if now - end < grace_ms:
        return "e:grace"
    return None


def scan(sess_dir, ws, agent, now=None):
    now = now or int(time.time() * 1000)
    timeout_s, timeout_src = run_timeout_s(agent)
    timeout_ms = timeout_s * 1000
    grace_ms = _env_int("STALL_GRACE_MIN", 40) * 60_000
    lookback_ms = _env_int("STALL_LOOKBACK_H", 24) * 3_600_000
    subs, notes = load_subagents(sess_dir, ws)
    rows = []
    for s in subs:
        cand, end, kind = is_candidate(s, now, timeout_ms)
        if not cand or now - end > lookback_ms:
            continue
        why = suppress_reason(s, end, subs, notes, now, timeout_ms, grace_ms)
        rows.append({"key": s["key"], "kind": kind, "endedAt": end, "label": s["label"],
                     "spawnedBy": s["spawnedBy"], "job": s["job"], "notes": s["notes"],
                     "sessionFile": s["sessionFile"], "suppressed": why})
    meta = {"run_timeout_s": timeout_s, "run_timeout_src": timeout_src,
            "grace_min": grace_ms // 60_000, "subagents": len(subs)}
    return rows, meta


# ── ledger: fire once per (key, endedAt); restarts counted per job per day ──
def ledger_load(path):
    try:
        d = json.load(open(path))
        if isinstance(d, dict):
            d.setdefault("handled", {}); d.setdefault("restarts", {})
            return d
    except Exception:
        pass
    return {"handled": {}, "restarts": {}}


def ledger_save(path, d):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)) or ".", prefix=".stall_ledger.")
    with os.fdopen(fd, "w") as f:
        json.dump(d, f, indent=1, sort_keys=True)
    os.replace(tmp, path)   # atomic: a crashed write never leaves half a ledger


def _lkey(key, ended):
    return f"{key}@{ended}"


def restarts_today(led, job, now):
    day = time.strftime("%Y-%m-%d", time.gmtime(now / 1000))
    return led["restarts"].get(job, {}).get(day, 0)


# ── commands ─────────────────────────────────────────────────────────────────
def cmd_gate(args, now=None):
    """Exit 0 + JSON on new stalls, else 1. An error exits 1 (logged to stderr):
    a broken store must neither wake the LLM every tick nor stop the other
    Note Cron Gate lanes — this lane just goes quiet until the store reads."""
    now = now or int(time.time() * 1000)
    ws, sess, agent, ledger = _paths()
    if not sess or not os.path.isfile(os.path.join(sess, "sessions.json")):
        print(f"stall_watch: no sessions.json under {sess!r}", file=sys.stderr)
        return 1
    rows, meta = scan(sess, ws, agent, now)
    led = ledger_load(ledger)
    fresh = [r for r in rows if r["suppressed"] is None
             and _lkey(r["key"], r["endedAt"]) not in led["handled"]]
    for r in fresh:
        r["restarts_today"] = restarts_today(led, r["job"], now)
        if not args.dry_run:
            led["handled"][_lkey(r["key"], r["endedAt"])] = {
                "state": "fired", "fired_at": now, "job": r["job"], "kind": r["kind"],
                "label": r["label"], "spawnedBy": r["spawnedBy"], "notes": r["notes"],
                "sessionFile": r["sessionFile"]}
    if fresh and not args.dry_run:
        ledger_save(ledger, led)
    out = {"stalls": fresh, "meta": meta}
    if args.verbose:
        out["all_candidates"] = rows
    print(json.dumps(out, indent=1))
    return 0 if fresh else 1


def cmd_pending(args, now=None):
    now = now or int(time.time() * 1000)
    _, _, _, ledger = _paths()
    led = ledger_load(ledger)
    mx = _env_int("STALL_MAX_RESTARTS", 2)
    out = []
    for lk, h in sorted(led["handled"].items()):
        if h.get("state") != "fired":
            continue
        key, _, ended = lk.rpartition("@")
        n = restarts_today(led, h.get("job", ""), now)
        out.append(dict(h, key=key, endedAt=int(ended), restarts_today=n,
                        give_up=n >= mx, tail=_tail_text(h.get("sessionFile", ""))))
    print(json.dumps({"pending": out, "max_restarts_per_day": mx}, indent=1))
    return 0


def cmd_resolve(args, now=None):
    now = now or int(time.time() * 1000)
    _, _, _, ledger = _paths()
    led = ledger_load(ledger)
    lk = _lkey(args.key, args.ended)
    h = led["handled"].setdefault(lk, {"job": args.job or ""})
    h.update(state=args.action, resolved_at=now)
    job = args.job or h.get("job", "")
    if args.action == "restarted" and job:
        day = time.strftime("%Y-%m-%d", time.gmtime(now / 1000))
        led["restarts"].setdefault(job, {})[day] = restarts_today(led, job, now) + 1
    # retention: a handled entry only matters while its session is still inside
    # the lookback window; restart counters only for today/yesterday.
    keep_ms = (_env_int("STALL_LOOKBACK_H", 24) + 48) * 3_600_000
    led["handled"] = {k: v for k, v in led["handled"].items()
                      if now - int(k.rpartition("@")[2] or 0) < keep_ms}
    ledger_save(ledger, led)
    print(json.dumps({"resolved": lk, "action": args.action, "job": job,
                      "restarts_today": restarts_today(led, job, now)}))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sp = ap.add_subparsers(dest="cmd", required=True)
    g = sp.add_parser("gate"); g.add_argument("--dry-run", action="store_true")
    g.add_argument("--verbose", action="store_true")
    sp.add_parser("pending")
    r = sp.add_parser("resolve"); r.add_argument("--key", required=True)
    r.add_argument("--ended", required=True, type=int); r.add_argument("--job", default="")
    r.add_argument("--action", required=True, choices=["restarted", "gaveup", "skipped"])
    sp.add_parser("selftest")
    a = ap.parse_args(argv)
    if a.cmd == "selftest":
        return selftest()
    try:
        return {"gate": cmd_gate, "pending": cmd_pending, "resolve": cmd_resolve}[a.cmd](a)
    except Exception as e:  # zero-LLM gate: never traceback into a cron run
        print(f"stall_watch: {a.cmd} error: {e!r}", file=sys.stderr)
        return 1


# ── selftest: synthetic sessions.json fixtures, one per rule ────────────────
def selftest():
    import contextlib, io, shutil
    MIN = 60_000
    NOW = 1_800_000_000_000
    tmp = tempfile.mkdtemp(prefix="stall_selftest_")
    ws, sess = os.path.join(tmp, "ws"), os.path.join(tmp, "sessions")
    os.makedirs(os.path.join(ws, "memory")); os.makedirs(sess)
    saved = {k: os.environ.get(k) for k in ("OPENCLAW_WORKSPACE", "DINOMEM_AGENT_SESSIONS",
             "STALL_LEDGER", "STALL_RUN_TIMEOUT_S", "STALL_GRACE_MIN", "STALL_LOOKBACK_H")}
    os.environ.update(OPENCLAW_WORKSPACE=ws, DINOMEM_AGENT_SESSIONS=sess,
                      STALL_LEDGER=os.path.join(tmp, "ledger.json"),
                      STALL_RUN_TIMEOUT_S="1800", STALL_GRACE_MIN="40", STALL_LOOKBACK_H="24")
    store, results = {}, []

    def note(name, status, mtime):
        p = os.path.join(ws, "memory", f"_note_{name}.md")
        open(p, "w").write(f"---\ntype: project\nstatus: {status}\n---\nbody\n")
        os.utime(p, (mtime / 1000, mtime / 1000))

    def sub(sid, status, started, ended=None, act=None, label="", parent="P", brief="", fmt=None):
        f = os.path.join(sess, f"{sid}.jsonl")
        open(f, "w").write(json.dumps({"type": "message", "message": {"role": "user", "content": brief}}) + "\n")
        fm = fmt or act or ended or started
        os.utime(f, (fm / 1000, fm / 1000))
        store[f"agent:x:subagent:{sid}"] = {"status": status, "startedAt": started, "endedAt": ended,
            "lastActivityAt": act, "label": label, "spawnedBy": parent, "sessionFile": f}

    H = 60 * MIN
    # each fixture gets its own parent so (d) only fires where intended
    sub("f-failed", "failed", NOW - 3*H, NOW - 2*H, NOW - 2*H, "lone failed", "P1")
    sub("f-timeout", "timeout", NOW - 4*H, NOW - 3*H, NOW - 3*H, "lone timeout", "P2")
    sub("f-ghost", "running", NOW - 5*H, None, None, "ghost run", "P3", fmt=NOW - 4*H)
    sub("f-alive", "running", NOW - 20*MIN, None, NOW - 5*MIN, "alive run", "P4")
    sub("f-done", "done", NOW - 3*H, NOW - 2*H, NOW - 2*H, "done run", "P5")
    sub("f-resumed", "failed", NOW - 3*H, NOW - 2*H, NOW - 1*H, "resumed", "P6")
    sub("f-sib-old", "failed", NOW - 5*H, NOW - 4*H, NOW - 4*H, "build thing", "P7")
    sub("f-sib-new", "done", NOW - 3*H, NOW - 2*H, NOW - 2*H, "build thing (retry)", "P7x")
    sub("f-sib-sameparent", "failed", NOW - 5*H, NOW - 4*H, NOW - 4*H, "sync logs", "P8")
    sub("f-sib-sp-new", "running", NOW - 30*MIN, None, NOW - 2*MIN, "sync logs (resume)", "P8")
    # overlapping sibling: started before the old one ended, so (d) cannot match
    sub("f-ov-old", "timeout", NOW - 4*H, NOW - 3*H, NOW - 3*H, "audit", "P15")
    sub("f-ov-new", "done", NOW - 3*H - 30*MIN, NOW - 2*H, NOW - 2*H, "audit (retry)", "P15")
    note("moved", "in_progress", NOW - 1*H)
    sub("f-note", "timeout", NOW - 4*H, NOW - 3*H, NOW - 3*H, "x", "P9", brief="go memory/_note_moved.md")
    note("closed", "done", NOW - 5*H)
    sub("f-notedone", "failed", NOW - 4*H, NOW - 3*H, NOW - 3*H, "y", "P10", brief="memory/_note_closed.md")
    note("frozen", "in_progress", NOW - 5*H)
    sub("f-notefrozen", "failed", NOW - 4*H, NOW - 3*H, NOW - 3*H, "z", "P11", brief="memory/_note_frozen.md")
    sub("f-parent-a", "failed", NOW - 4*H, NOW - 3*H, NOW - 3*H, "alpha", "P12")
    sub("f-parent-b", "failed", NOW - 2*H, NOW - 1*H - 50*MIN, NOW - 1*H - 50*MIN, "beta", "P12")
    sub("f-grace", "failed", NOW - 1*H, NOW - 10*MIN, NOW - 10*MIN, "grace", "P13")
    sub("f-old", "failed", NOW - 50*H, NOW - 49*H, NOW - 49*H, "too old", "P14")
    json.dump(store, open(os.path.join(sess, "sessions.json"), "w"))

    def check(name, cond):
        results.append((name, bool(cond)))

    try:
        rows, meta = scan(sess, ws, "x", NOW)
        by = {r["key"].rsplit(":", 1)[1]: r for r in rows}
        def is_stall(sid): return sid in by and by[sid]["suppressed"] is None
        def why(sid): return (by.get(sid) or {}).get("suppressed") or ""
        check("failed -> stall", is_stall("f-failed"))
        check("timeout -> stall", is_stall("f-timeout"))
        check("ghost running (silent > 2x timeout) -> stall", is_stall("f-ghost") and by["f-ghost"]["kind"] == "ghost")
        check("running + alive -> not a candidate", "f-alive" not in by)
        check("done -> never a candidate", "f-done" not in by)
        check("(a) same session resumed -> suppressed", why("f-resumed").startswith("a:"))
        check("(b) newer sibling by normalized label is NOT matched across parents", is_stall("f-sib-old"))
        check("(b) newer sibling same parent running+alive -> suppressed", why("f-sib-sameparent").startswith("b:"))
        check("(b) overlapping newer sibling done -> suppressed", why("f-ov-old").startswith("b:newer-sibling-done"))
        check("(c) note moved after end -> suppressed", why("f-note").startswith("c:note-moved"))
        check("(c) note done -> suppressed", why("f-notedone").startswith("c:note-done"))
        check("(c) frozen in_progress note -> stall (the root gap)", is_stall("f-notefrozen"))
        check("(d) parent spawned after end -> suppressed", why("f-parent-a").startswith("d:"))
        check("(e) inside grace window -> suppressed", why("f-grace") == "e:grace")
        check("older than lookback -> ignored", "f-old" not in by)
        check("runTimeout from env", meta["run_timeout_s"] == 1800)
        # ledger: fire once, not twice; a NEW death of the same job fires again
        ns = argparse.Namespace(dry_run=False, verbose=False)
        with contextlib.redirect_stdout(io.StringIO()) as o1:
            rc1 = cmd_gate(ns, NOW)
        first = json.loads(o1.getvalue())["stalls"]
        with contextlib.redirect_stdout(io.StringIO()) as o2:
            rc2 = cmd_gate(ns, NOW + MIN)
        check("gate fires (exit 0) with stalls", rc1 == 0 and len(first) >= 4)
        check("ledger: same stalls do NOT fire twice (exit 1)", rc2 == 1 and json.loads(o2.getvalue())["stalls"] == [])
        store["agent:x:subagent:f-failed"]["endedAt"] = NOW - 1*H   # died again later
        store["agent:x:subagent:f-failed"]["lastActivityAt"] = NOW - 1*H
        json.dump(store, open(os.path.join(sess, "sessions.json"), "w"))
        with contextlib.redirect_stdout(io.StringIO()) as o3:
            rc3 = cmd_gate(ns, NOW + 2*MIN)
        again = [r["key"] for r in json.loads(o3.getvalue())["stalls"]]
        check("ledger: same job dying again later = new stall", rc3 == 0 and again == ["agent:x:subagent:f-failed"])
        # restart budget
        job = first[0]["job"]
        with contextlib.redirect_stdout(io.StringIO()):
            for _ in range(2):
                cmd_resolve(argparse.Namespace(key=first[0]["key"], ended=first[0]["endedAt"],
                                               job=job, action="restarted"), NOW)
            cmd_pending(argparse.Namespace(), NOW)
        led = ledger_load(os.environ["STALL_LEDGER"])
        check("restart counter per job per day", restarts_today(led, job, NOW) == 2)
        # dry-run writes nothing; missing store fails quiet (exit 1)
        os.remove(os.environ["STALL_LEDGER"])
        with contextlib.redirect_stdout(io.StringIO()):
            cmd_gate(argparse.Namespace(dry_run=True, verbose=False), NOW)
        check("--dry-run leaves no ledger", not os.path.exists(os.environ["STALL_LEDGER"]))
        os.environ["DINOMEM_AGENT_SESSIONS"] = os.path.join(tmp, "nope")
        with contextlib.redirect_stderr(io.StringIO()):
            check("missing sessions.json -> exit 1, no crash", cmd_gate(ns, NOW) == 1)
        check("norm_label strips retry/resume suffixes",
              norm_label("Logs sync (retry 2)") == norm_label("logs sync resume from note") == "logs sync")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)
    for n, ok in results:
        print(("PASS " if ok else "FAIL ") + n)
    bad = sum(1 for _, ok in results if not ok)
    print(f"{len(results) - bad}/{len(results)} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
