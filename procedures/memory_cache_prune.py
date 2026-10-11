#!/usr/bin/env python3
"""
memory_cache_prune.py — reclaim orphaned rows from memory_embedding_cache.

WHY THIS EXISTS
---------------
OpenClaw caches one embedding per content hash in `memory_embedding_cache` so
re-indexing unchanged text is free. Nothing ever removes a row whose content is
gone: the only DELETE in the engine (manager, full-reindex path) swaps the whole
table from a staging schema, it is not an age/ownership prune. So the table is
append-only in practice and grows without bound.

Measured on a live box 2026-10-11 (analyst agent): 82,471 cache rows backing
5,239 live chunks — 93% orphaned, 386MB of a 465MB DB. After pruning + VACUUM
the same DB was 83MB. Every dinomem install hits this; it is not box-specific.

WHAT COUNTS AS ORPHAN
---------------------
A cache row whose `hash` is not referenced by ANY row in `memory_index_chunks`.
Deleting one is cheap and reversible: if that content ever returns, it is simply
re-embedded (~100ms). We never key on AGE — an old hash that is still indexed is
still useful, and a fresh hash for deleted content is still garbage.

SAFETY
------
- Refuses to run when `memory_index_chunks` is EMPTY unless --force. An empty
  index means "not built yet / mid-rebuild", not "everything is orphaned"; in
  that state every cache row looks orphaned and a naive prune nukes a warm cache
  the next index pass would have reused. Measured: one agent on this box
  (wasales) really does sit at chunks=0, so this is a live path, not a theory.
- VACUUM is opt-out (--no-vacuum). Without it the pages are freed inside the
  file but the file itself never shrinks, which is the whole point.
- --dry-run reports exactly what would go, touching nothing.
"""
import argparse
import os
import sqlite3
import sys
from pathlib import Path

# A long busy timeout, not a short one: the gateway writes to this DB
# concurrently (WAL), and a prune that aborts halfway is worse than one that
# waits its turn.
BUSY_TIMEOUT_MS = 60_000

ORPHAN_WHERE = (
    "hash NOT IN (SELECT hash FROM memory_index_chunks)"
)


def discover_agent_dbs(openclaw_dir: Path):
    """Yield (agent_id, db_path) for every agent DB on this box.

    Mirrors resolve_memory_db() in scripts/install.sh: modern layout first
    (agents/<id>/agent/openclaw-agent.sqlite), legacy second (memory/<id>.sqlite).
    Kept deliberately dumb — discovery by globbing what EXISTS beats guessing
    agent ids from config, which is the mistake install.sh documents.
    """
    seen = set()
    for db in sorted((openclaw_dir / "agents").glob("*/agent/openclaw-agent.sqlite")):
        agent = db.parts[-3]
        real = db.resolve()
        if real in seen:
            continue
        seen.add(real)
        yield agent, db
    for db in sorted((openclaw_dir / "memory").glob("*.sqlite")):
        real = db.resolve()
        if real in seen:  # symlink into agents/ — already yielded
            continue
        seen.add(real)
        yield db.stem, db


def table_exists(conn, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (name,)
    ).fetchone()
    return row is not None


def prune_db(db_path: Path, *, dry_run: bool, vacuum: bool, force: bool):
    """Prune one DB. Returns a dict summary (never raises on normal misses)."""
    out = {"db": str(db_path), "status": "ok", "before": 0, "orphans": 0,
           "after": 0, "bytes_before": 0, "bytes_after": 0}
    if not db_path.exists():
        out["status"] = "missing"
        return out
    out["bytes_before"] = db_path.stat().st_size

    conn = sqlite3.connect(str(db_path), timeout=BUSY_TIMEOUT_MS / 1000)
    try:
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        if not table_exists(conn, "memory_embedding_cache") or not table_exists(
            conn, "memory_index_chunks"
        ):
            out["status"] = "no-cache-table"
            return out

        out["before"] = conn.execute(
            "SELECT count(*) FROM memory_embedding_cache"
        ).fetchone()[0]
        chunks = conn.execute(
            "SELECT count(*) FROM memory_index_chunks"
        ).fetchone()[0]
        out["chunks"] = chunks

        # See SAFETY: empty index != everything orphaned.
        if chunks == 0 and not force:
            out["status"] = "skipped-empty-index"
            return out

        out["orphans"] = conn.execute(
            f"SELECT count(*) FROM memory_embedding_cache WHERE {ORPHAN_WHERE}"
        ).fetchone()[0]

        if dry_run:
            out["status"] = "dry-run"
            out["after"] = out["before"] - out["orphans"]
            out["bytes_after"] = out["bytes_before"]
            return out

        if out["orphans"]:
            conn.execute(f"DELETE FROM memory_embedding_cache WHERE {ORPHAN_WHERE}")
            conn.commit()
        out["after"] = conn.execute(
            "SELECT count(*) FROM memory_embedding_cache"
        ).fetchone()[0]

        if vacuum and out["orphans"]:
            # VACUUM cannot run inside a transaction; isolation_level=None drops
            # python's implicit BEGIN.
            conn.isolation_level = None
            conn.execute("VACUUM")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error as exc:
        out["status"] = f"error: {exc}"
    finally:
        conn.close()

    out["bytes_after"] = db_path.stat().st_size if db_path.exists() else 0
    return out


def fmt_mb(n: int) -> str:
    return f"{n / 1024 / 1024:.0f}MB"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--db", help="prune ONE database file (default: every agent DB)")
    ap.add_argument("--openclaw-dir", default=os.environ.get("OPENCLAW_DIR", str(Path.home() / ".openclaw")))
    ap.add_argument("--dry-run", action="store_true", help="report only, change nothing")
    ap.add_argument("--no-vacuum", action="store_true", help="skip VACUUM (file will not shrink)")
    ap.add_argument("--force", action="store_true", help="prune even when the chunk index is empty")
    args = ap.parse_args()

    if args.db:
        targets = [(Path(args.db).stem, Path(args.db))]
    else:
        targets = list(discover_agent_dbs(Path(args.openclaw_dir)))

    if not targets:
        print("No agent databases found.", file=sys.stderr)
        return 1

    total_freed = 0
    total_orphans = 0
    for agent, db in targets:
        r = prune_db(db, dry_run=args.dry_run, vacuum=not args.no_vacuum, force=args.force)
        if r["status"] in ("missing", "no-cache-table"):
            continue
        if r["status"].startswith("error"):
            print(f"  {agent:<12} {r['status']}")
            continue
        if r["status"] == "skipped-empty-index":
            print(f"  {agent:<12} SKIP (chunk index empty; --force to override)")
            continue
        freed = r["bytes_before"] - r["bytes_after"]
        total_freed += freed
        total_orphans += r["orphans"]
        tag = " [dry-run]" if args.dry_run else ""
        print(
            f"  {agent:<12} cache {r['before']:>6} -> {r['after']:>6}"
            f"  orphans={r['orphans']:>6}"
            f"  {fmt_mb(r['bytes_before'])} -> {fmt_mb(r['bytes_after'])}{tag}"
        )

    verb = "would free" if args.dry_run else "freed"
    print(f"Total: {total_orphans} orphaned embeddings, {verb} {fmt_mb(total_freed)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
