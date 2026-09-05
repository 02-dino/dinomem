#!/usr/bin/env python3
"""
_retrieval_log.py — base-owned, scale-first retrieval observability for dinomem.

WHY: dinomem grew multiple retrieval tools (session_search, docs_search,
graph_search, data_query, + native memory_search). Tool *calls* are invisible:
you can't audit WHICH tool ran, on WHAT query, returning WHAT scores, or whether
it fell back / mis-routed. This is the read-side twin of _memory_diff.py (which
logs memory WRITES). Together they make memory fully observable.

DESIGN (scale-first — retrieval is high-frequency and grows forever):
  - Storage: ONE daily-rotated JSONL per day  -> kb/retrieval_log/YYYY-MM-DD.jsonl
    (NOT one-file-per-call — that explodes inode count). Append-only, one line
    per retrieval call. POSIX append of a <4KB line is atomic, so concurrent
    tools don't need locking.
  - Retention: raw JSONL kept RAW_RETENTION_DAYS (default 30). The rollup step
    compresses older dailies into kb/retrieval_log/rollups/YYYY-MM.json (counts,
    fallback rate, avg top-score per tool) and deletes the raw. Bounded raw
    footprint, permanent compressed signal.
  - Health: rollup writes kb/retrieval_log/health.json — a small, always-current
    routing-quality snapshot the skill/router can read.
  - FAIL-OPEN: every public call is wrapped; a logging error must NEVER break a
    retrieval. Stdlib only (json, os, pathlib, datetime, glob). Noob-install-clean.

PUBLIC API:
  log_retrieval(workspace, tool, query, k=None, result_uris=None,
                top_scores=None, fallback_used=False, latency_ms=None,
                extra=None, log_fn=None) -> bool
      Append one retrieval event. Call it from each neuron retrieval tool.

  rollup(workspace, retention_days=RAW_RETENTION_DAYS, log_fn=None) -> dict
      Roll up + prune old dailies, refresh health.json. Call from the daily cron.

CLI (inspector):
  python3 _retrieval_log.py --list [--limit N]          # recent events (today+)
  python3 _retrieval_log.py --tool graph_search         # filter by tool
  python3 _retrieval_log.py --health                    # print health.json
  python3 _retrieval_log.py --rollup [--retention-days N]
"""
import os
import re
import sys
import json
import glob
from pathlib import Path
from datetime import datetime, timedelta, timezone

RAW_RETENTION_DAYS = 30
MAX_QUERY_CHARS = 500
MAX_URIS = 20
LOG_SUBDIR = "kb/retrieval_log"
ROLLUP_SUBDIR = "kb/retrieval_log/rollups"


def _now_utc():
    return datetime.now(timezone.utc)


def _log_dir(workspace):
    return Path(workspace) / LOG_SUBDIR


def _rollup_dir(workspace):
    return Path(workspace) / ROLLUP_SUBDIR


def _clip_query(q):
    if q is None:
        return ""
    q = str(q)
    if len(q) > MAX_QUERY_CHARS:
        return q[:MAX_QUERY_CHARS] + f"…[+{len(q) - MAX_QUERY_CHARS}]"
    return q


def _norm_uris(uris):
    if not uris:
        return []
    out = []
    for u in list(uris)[:MAX_URIS]:
        try:
            out.append(str(u))
        except Exception:
            pass
    return out


def _norm_scores(scores):
    if not scores:
        return []
    out = []
    for s in list(scores)[:MAX_URIS]:
        try:
            out.append(round(float(s), 4))
        except Exception:
            pass
    return out


def log_retrieval(workspace, tool, query, k=None, result_uris=None,
                  top_scores=None, fallback_used=False, latency_ms=None,
                  extra=None, log_fn=None, source="tool"):
    """Append ONE retrieval event to today's JSONL. Fail-open: never raises.

    `source` distinguishes WHERE the call came from so reports don't read as
    analyst-biased: "tool" = an explicit dinomem python retrieval tool
    (hybrid_recall/session_search/...), "native" = an OpenClaw gateway memory
    tool (memory_search/memory_get) captured by the dinomem-retrieval-log plugin.
    Default "tool" keeps every existing caller backward-compatible.
    """
    _lf = log_fn or (lambda m: None)
    try:
        d = _log_dir(workspace)
        d.mkdir(parents=True, exist_ok=True)
        now = _now_utc()
        day = now.strftime("%Y-%m-%d")
        rec = {
            "ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "tool": str(tool),
            "source": str(source or "tool"),
            "query": _clip_query(query),
            "k": k,
            "n_results": len(_norm_uris(result_uris)),
            "top_scores": _norm_scores(top_scores),
            "result_uris": _norm_uris(result_uris),
            "fallback_used": bool(fallback_used),
        }
        if latency_ms is not None:
            try:
                rec["latency_ms"] = int(latency_ms)
            except Exception:
                pass
        if extra and isinstance(extra, dict):
            # keep extra small & json-safe
            try:
                json.dumps(extra)
                rec["extra"] = extra
            except Exception:
                pass
        line = json.dumps(rec, ensure_ascii=False)
        # POSIX atomic append for a single <4KB line — no lock needed.
        fp = d / f"{day}.jsonl"
        with open(fp, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        return True
    except Exception as e:
        _lf(f"   ⚠️  retrieval_log.log_retrieval failed (non-fatal): {e}")
        return False


def _read_jsonl(fp):
    out = []
    try:
        with open(fp, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    except Exception:
        pass
    return out


def _summarize_records(records):
    """Compute per-tool routing-quality metrics from a list of event dicts."""
    per_tool = {}
    total = 0
    for r in records:
        total += 1
        tool = r.get("tool", "unknown")
        t = per_tool.setdefault(tool, {
            "calls": 0, "fallbacks": 0, "empty_results": 0,
            "_score_sum": 0.0, "_score_n": 0,
        })
        t["calls"] += 1
        if r.get("fallback_used"):
            t["fallbacks"] += 1
        if not r.get("n_results"):
            t["empty_results"] += 1
        ts = r.get("top_scores") or []
        if ts:
            t["_score_sum"] += float(ts[0])
            t["_score_n"] += 1
    # finalize
    for tool, t in per_tool.items():
        n = t.pop("_score_n")
        s = t.pop("_score_sum")
        t["avg_top_score"] = round(s / n, 4) if n else None
        t["fallback_rate"] = round(t["fallbacks"] / t["calls"], 4) if t["calls"] else 0.0
        t["empty_rate"] = round(t["empty_results"] / t["calls"], 4) if t["calls"] else 0.0
    return {"total_calls": total, "per_tool": per_tool}


def rollup(workspace, retention_days=RAW_RETENTION_DAYS, log_fn=None):
    """Compress dailies older than retention_days into monthly rollups + prune raw;
    refresh health.json from the most recent window. Fail-open: never raises."""
    _lf = log_fn or (lambda m: None)
    result = {"rolled_months": [], "pruned_days": 0, "health_written": False}
    try:
        d = _log_dir(workspace)
        rd = _rollup_dir(workspace)
        if not d.exists():
            return result
        rd.mkdir(parents=True, exist_ok=True)
        cutoff = (_now_utc() - timedelta(days=retention_days)).strftime("%Y-%m-%d")

        # 1) group old dailies by month
        by_month = {}
        for fp in sorted(d.glob("*.jsonl")):
            day = fp.stem  # YYYY-MM-DD
            if len(day) != 10:
                continue
            if day < cutoff:
                by_month.setdefault(day[:7], []).append(fp)

        # 2) fold each old month into rollup + delete raw
        for month, files in sorted(by_month.items()):
            recs = []
            for fp in files:
                recs.extend(_read_jsonl(fp))
            summ = _summarize_records(recs)
            roll_fp = rd / f"{month}.json"
            existing = {}
            if roll_fp.exists():
                try:
                    existing = json.loads(roll_fp.read_text(encoding="utf-8"))
                except Exception:
                    existing = {}
            merged = _merge_summaries(existing.get("summary"), summ)
            roll_fp.write_text(json.dumps({
                "month": month,
                "updated_at": _now_utc().strftime("%Y-%m-%dT%H:%M:%SZ"),
                "summary": merged,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            for fp in files:
                try:
                    fp.unlink()
                    result["pruned_days"] += 1
                except Exception:
                    pass
            result["rolled_months"].append(month)

        # 3) refresh health.json from the RAW window still on disk (recent signal)
        recent = []
        for fp in sorted(d.glob("*.jsonl")):
            recent.extend(_read_jsonl(fp))
        health = _summarize_records(recent)
        health["window"] = f"last_{retention_days}d_raw"
        health["generated_at"] = _now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")
        (d / "health.json").write_text(
            json.dumps(health, ensure_ascii=False, indent=2), encoding="utf-8")
        result["health_written"] = True
        _lf(f"   🧾 retrieval_log rollup: {len(result['rolled_months'])} month(s), "
            f"pruned {result['pruned_days']} daily file(s), health refreshed")
        return result
    except Exception as e:
        _lf(f"   ⚠️  retrieval_log.rollup failed (non-fatal): {e}")
        return result


def _merge_summaries(a, b):
    """Additively merge two summarize_records() outputs (for monthly accumulation)."""
    if not a:
        return b
    if not b:
        return a
    out = {"total_calls": a.get("total_calls", 0) + b.get("total_calls", 0), "per_tool": {}}
    tools = set(a.get("per_tool", {})) | set(b.get("per_tool", {}))
    for tool in tools:
        ta = a.get("per_tool", {}).get(tool, {})
        tb = b.get("per_tool", {}).get(tool, {})
        calls = ta.get("calls", 0) + tb.get("calls", 0)
        fbk = ta.get("fallbacks", 0) + tb.get("fallbacks", 0)
        emp = ta.get("empty_results", 0) + tb.get("empty_results", 0)
        # weighted avg of avg_top_score by score-bearing calls is lost after
        # finalize; approximate by call-weighted avg of the two averages.
        sa, sb = ta.get("avg_top_score"), tb.get("avg_top_score")
        if sa is not None and sb is not None:
            ca = ta.get("calls", 1) or 1
            cb = tb.get("calls", 1) or 1
            avg = round((sa * ca + sb * cb) / (ca + cb), 4)
        else:
            avg = sa if sa is not None else sb
        out["per_tool"][tool] = {
            "calls": calls, "fallbacks": fbk, "empty_results": emp,
            "avg_top_score": avg,
            "fallback_rate": round(fbk / calls, 4) if calls else 0.0,
            "empty_rate": round(emp / calls, 4) if calls else 0.0,
        }
    return out


# ---------------------------------------------------------------- CLI inspector
def _cli(argv):
    import argparse
    ap = argparse.ArgumentParser(description="dinomem retrieval-log inspector")
    ap.add_argument("--workspace", default=os.environ.get(
        "DINOMEM_WORKSPACE", str(Path(__file__).resolve().parent.parent)))
    ap.add_argument("--list", action="store_true", help="show recent raw events")
    ap.add_argument("--tool", help="filter --list by tool name")
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--health", action="store_true", help="print health.json")
    ap.add_argument("--rollup", action="store_true", help="run rollup+prune now")
    ap.add_argument("--retention-days", type=int, default=RAW_RETENTION_DAYS)
    # --- record path: append ONE event from outside Python (the TS plugin shells
    #     here fire-and-forget so native memory_search/get land in the SAME log via
    #     the SAME writer — no forked JSONL schema). All record args are optional. ---
    ap.add_argument("--record", action="store_true",
                    help="append one retrieval event (for the native-recall plugin)")
    ap.add_argument("--query", help="record: the query text")
    ap.add_argument("--k", type=int, help="record: requested k")
    ap.add_argument("--n-results", type=int, help="record: result count (native has no uris)")
    ap.add_argument("--result-uris", help="record: comma/newline-separated uris")
    ap.add_argument("--source", default="tool", help="record: native|tool (default tool)")
    ap.add_argument("--extra-json", help="record: small json-object of extra fields")
    a = ap.parse_args(argv)
    ws = a.workspace
    d = _log_dir(ws)

    if a.record:
        uris = None
        if a.result_uris:
            uris = [u for u in re.split(r"[,\n]", a.result_uris) if u.strip()]
        # native tools expose a COUNT but not the uri list -> synthesize N blanks so
        # n_results is correct without inventing fake uris.
        elif a.n_results:
            uris = [""] * max(0, a.n_results)
        extra = None
        if a.extra_json:
            try:
                parsed = json.loads(a.extra_json)
                if isinstance(parsed, dict):
                    extra = parsed
            except Exception:
                pass
        ok = log_retrieval(ws, a.tool or "native", a.query or "", k=a.k,
                           result_uris=uris, source=a.source, extra=extra)
        print(json.dumps({"ok": bool(ok)}))
        return 0 if ok else 1

    if a.rollup:
        r = rollup(ws, retention_days=a.retention_days, log_fn=print)
        print(json.dumps(r, indent=2))
        return 0

    if a.health:
        hp = d / "health.json"
        if not hp.exists():
            print("No health.json yet — run --rollup first.")
            return 1
        print(hp.read_text(encoding="utf-8"))
        return 0

    # default / --list : most recent events across raw dailies
    recs = []
    for fp in sorted(d.glob("*.jsonl"), reverse=True):
        recs = _read_jsonl(fp) + recs
        if len(recs) >= a.limit * 4:
            break
    if a.tool:
        recs = [r for r in recs if r.get("tool") == a.tool]
    recs = recs[-a.limit:]
    if not recs:
        print("No retrieval events logged yet.")
        return 0
    for r in recs:
        scores = r.get("top_scores") or []
        top = scores[0] if scores else "—"
        fb = " ⤵fallback" if r.get("fallback_used") else ""
        print(f"{r.get('ts','')}  {r.get('tool',''):<14} "
              f"n={r.get('n_results',0)} top={top}{fb}  q={r.get('query','')[:60]!r}")
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
