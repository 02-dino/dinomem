#!/usr/bin/env python3
"""
_memory_diff.py — per-extraction memory change audit log (base-owned shared helper).

Ported concept from OpenViking's memory_diff.json: every extraction run records
exactly which memory item-files were ADDED / UPDATED / DELETED, with before/after
content, so any memory mutation (not just L4 promotions) is auditable and reversible.

Design constraints (dinomem philosophy):
  - ZERO new dependencies (stdlib only) — base must stay noob-install-clean.
  - NON-DESTRUCTIVE: only writes an audit log; never touches memory/*.md itself.
  - FAIL-OPEN: any error here must NEVER break extraction. All public calls are
    wrapped so a diff-logging failure logs a warning and returns silently.
  - SHARED: imported by BOTH base and neuron extract_memory.py (neuron's installer
    overwrites extract_memory.py but this helper is base-owned; neuron ships an
    identical copy or leaves base's in place — either way the import resolves).

Output: memory/.diffs/YYYY-MM-DD_HHMMSS_<pid>.json  (one file per extraction run)
  {
    "run_id": "...", "extracted_at": "ISO8601", "date": "YYYY-MM-DD",
    "operations": {
      "adds":    [{"uri": "memory/xxx.md", "item_type": "...", "after": "..."}],
      "updates": [{"uri": "...", "item_type": "...", "before": "...", "after": "..."}],
      "deletes": [{"uri": "...", "item_type": "...", "deleted_content": "..."}]
    },
    "summary": {"total_adds": N, "total_updates": N, "total_deletes": N}
  }

An empty diff (all zero) is still written, matching OpenViking semantics — a run
that changed nothing leaves proof it ran and touched nothing.

Usage (from extract_memory.py):
    from _memory_diff import MemoryDiff
    diff = MemoryDiff(MEMORY_DIR, date_str=today)
    ...
    diff.record_add(fpath, item_type, content)        # on new file write
    diff.record_update(fpath, item_type, before, after)  # on in-place merge/compact
    diff.record_delete(fpath, item_type, deleted)     # on removal
    ...
    diff.flush()   # writes the JSON; safe to call once at end of run
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

DIFF_DIRNAME = ".diffs"
# Keep the audit trail bounded so it never becomes the bloat it audits.
# Default retention: keep the most recent N diff files, prune older ones on flush.
MAX_DIFF_FILES = int(os.environ.get("DINOMEM_MAX_DIFF_FILES", "500") or "500")
# Cap stored content length per side so a huge memory item can't balloon the log.
MAX_CONTENT_CHARS = int(os.environ.get("DINOMEM_DIFF_MAX_CHARS", "8000") or "8000")


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rel(memory_dir, path):
    """Store a workspace-relative-ish uri (memory/<name>) for portability."""
    try:
        p = Path(path)
        name = p.name
        return f"memory/{name}"
    except Exception:
        return str(path)


def _clip(text):
    if text is None:
        return ""
    text = str(text)
    if len(text) > MAX_CONTENT_CHARS:
        return text[:MAX_CONTENT_CHARS] + f"\n…[clipped {len(text) - MAX_CONTENT_CHARS} chars]"
    return text


class MemoryDiff:
    """Accumulates memory mutations for one extraction run, then flushes one JSON."""

    def __init__(self, memory_dir, date_str=None, log_fn=None):
        self.memory_dir = Path(memory_dir)
        self.date_str = date_str or datetime.now().strftime("%Y-%m-%d")
        self._log = log_fn or (lambda m: None)
        self.adds = []
        self.updates = []
        self.deletes = []
        self._flushed = False
        self.run_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{os.getpid()}"

    # ---- recording (all fail-open) ----------------------------------------
    def record_add(self, path, item_type, content):
        try:
            self.adds.append({
                "uri": _rel(self.memory_dir, path),
                "item_type": item_type or "",
                "after": _clip(content),
            })
        except Exception as e:
            self._log(f"   ⚠️  memory_diff.record_add failed: {e}")

    def record_update(self, path, item_type, before, after):
        try:
            self.updates.append({
                "uri": _rel(self.memory_dir, path),
                "item_type": item_type or "",
                "before": _clip(before),
                "after": _clip(after),
            })
        except Exception as e:
            self._log(f"   ⚠️  memory_diff.record_update failed: {e}")

    def record_delete(self, path, item_type, deleted_content):
        try:
            self.deletes.append({
                "uri": _rel(self.memory_dir, path),
                "item_type": item_type or "",
                "deleted_content": _clip(deleted_content),
            })
        except Exception as e:
            self._log(f"   ⚠️  memory_diff.record_delete failed: {e}")

    def has_changes(self):
        return bool(self.adds or self.updates or self.deletes)

    # ---- flush ------------------------------------------------------------
    def flush(self):
        """Write the diff JSON. Fail-open: never raises. Returns path or None."""
        if self._flushed:
            return None
        self._flushed = True
        try:
            diff_dir = self.memory_dir / DIFF_DIRNAME
            diff_dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "run_id": self.run_id,
                "extracted_at": _now_iso(),
                "date": self.date_str,
                "operations": {
                    "adds": self.adds,
                    "updates": self.updates,
                    "deletes": self.deletes,
                },
                "summary": {
                    "total_adds": len(self.adds),
                    "total_updates": len(self.updates),
                    "total_deletes": len(self.deletes),
                },
            }
            out = diff_dir / f"{self.run_id}.json"
            tmp = diff_dir / f".{self.run_id}.json.tmp"
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(out)  # atomic
            self._prune(diff_dir)
            n = len(self.adds) + len(self.updates) + len(self.deletes)
            self._log(f"   🧾 memory_diff: {len(self.adds)} add / {len(self.updates)} upd / "
                      f"{len(self.deletes)} del → {DIFF_DIRNAME}/{out.name}")
            return out
        except Exception as e:
            self._log(f"   ⚠️  memory_diff.flush failed (non-fatal): {e}")
            return None

    def _prune(self, diff_dir):
        try:
            files = sorted(
                (f for f in diff_dir.glob("*.json")),
                key=lambda f: f.stat().st_mtime,
            )
            excess = len(files) - MAX_DIFF_FILES
            for f in files[:max(0, excess)]:
                try:
                    f.unlink()
                except Exception:
                    pass
        except Exception:
            pass


# --- CLI: inspect / list diffs -------------------------------------------------
def _cli(argv):
    import argparse
    ap = argparse.ArgumentParser(description="Inspect dinomem memory diffs.")
    ap.add_argument("memory_dir", nargs="?", default="memory",
                    help="Path to memory/ dir (default: ./memory)")
    ap.add_argument("--list", action="store_true", help="List diff files newest-first")
    ap.add_argument("--show", help="Show a specific diff run_id (or filename)")
    ap.add_argument("--limit", type=int, default=10)
    args = ap.parse_args(argv)

    diff_dir = Path(args.memory_dir) / DIFF_DIRNAME
    if not diff_dir.exists():
        print(f"No diffs found at {diff_dir}")
        return 0

    if args.show:
        name = args.show if args.show.endswith(".json") else f"{args.show}.json"
        p = diff_dir / name
        if not p.exists():
            print(f"Not found: {p}")
            return 1
        print(p.read_text(encoding="utf-8"))
        return 0

    files = sorted(diff_dir.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
    for f in files[:args.limit]:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            s = d.get("summary", {})
            print(f"{f.name}  {d.get('extracted_at','')}  "
                  f"+{s.get('total_adds',0)} ~{s.get('total_updates',0)} -{s.get('total_deletes',0)}")
        except Exception as e:
            print(f"{f.name}  (unreadable: {e})")
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
