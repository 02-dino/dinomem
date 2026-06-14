#!/usr/bin/env python3
"""
memory_cleanup.py — Deduplication + bootcheck cleanup + MEMORY.md size trim.

What it does:
1. Semantic dedup: embeds all memory items via TEI, clusters by cosine similarity,
   merges each cluster into one canonical item via LLM. Falls back to string dedup
   if TEI is unavailable.
2. Auto-deduplicates known framework recitation (workflow_market, analysis_template, etc.)
3. Removes redundant bootcheck-only files (no new facts, just framework recitation)
4. Trims MEMORY.md index if over MAX_INDEX_CHARS — removes oldest entries only.
   Raw data in memory/*.md is never touched. Index can be rebuilt anytime.

Covers all item categories: [factual], [pattern], [operational], [decision], [correction],
[preference], [uncertain], [lesson].

What it does NOT do: stale data flagging, prediction expiry, uncertain cleanup.
That is handled by memory_review.py and OpenClaw's own memory system.

Run: python3 procedures/memory_cleanup.py
"""

import json
import re
import urllib.request
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

WORKSPACE = Path("DINOMEM_WORKSPACE_PLACEHOLDER")
MEMORY_DIR = WORKSPACE / "memory"
ARCHIVE_DIR = WORKSPACE / ".memory_archive"
MEMORY_INDEX = WORKSPACE / "MEMORY.md"

SIM_THRESHOLD = 0.80          # string similarity fallback
SEMANTIC_THRESHOLD = 0.88     # cosine similarity for semantic dedup
MAX_INDEX_CHARS = 18000       # 90% of default maxBootstrapFileChars (20000)
TEI_URL = "http://localhost:8080/v1/embeddings"
ALL_ITEM_TAGS = r'\[factual\]|\[pattern\]|\[operational\]|\[decision\]|\[correction\]|\[preference\]|\[uncertain\]|\[lesson\]|\[prediction\]'

KNOWN_FRAMEWORK_FACTS = [
    "framework validation: The AI successfully recalled",
    "framework validation: The AI correctly identified",
    "user expects exact adherence to instructions",
    "user expects structured and detailed recall",
]

def similar(a, b):
    return SequenceMatcher(None, a, b).ratio()

def is_framework_noise(text):
    text_lower = text.lower()
    return any(known.lower() in text_lower for known in KNOWN_FRAMEWORK_FACTS)

def is_duplicate(text, seen_facts):
    if is_framework_noise(text):
        return True
    for seen in seen_facts:
        if similar(text, seen) >= SIM_THRESHOLD:
            return True
    return False

# ── Semantic Dedup via TEI ────────────────────────────────────────────────────────────
def get_embeddings(texts):
    """Get embeddings from TEI. Returns list of vectors or None if unavailable."""
    try:
        payload = json.dumps({"input": texts, "model": ""}).encode()
        req = urllib.request.Request(TEI_URL, data=payload,
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        return [item["embedding"] for item in data["data"]]
    except Exception:
        return None

def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0

def cluster_items(items, embeddings):
    """Greedy clustering by cosine similarity. Returns list of clusters (each a list of indices)."""
    assigned = [-1] * len(items)
    clusters = []
    for i in range(len(items)):
        if assigned[i] >= 0:
            continue
        cluster = [i]
        assigned[i] = len(clusters)
        for j in range(i + 1, len(items)):
            if assigned[j] >= 0:
                continue
            if cosine(embeddings[i], embeddings[j]) >= SEMANTIC_THRESHOLD:
                cluster.append(j)
                assigned[j] = len(clusters)
        clusters.append(cluster)
    return clusters

def merge_cluster(items_in_cluster):
    """Keep the longest/most informative item from a cluster (no LLM needed for simple cases)."""
    return max(items_in_cluster, key=len)

def semantic_dedup_items(items):
    """
    Given a list of item strings, return deduplicated list using TEI embeddings.
    Falls back to string dedup if TEI unavailable.
    Returns (deduped_items, removed_count).
    """
    if not items:
        return items, 0
    # Filter framework noise first
    clean = [(i, t) for i, t in enumerate(items) if not is_framework_noise(t)]
    noisy = [t for _, t in [(i, t) for i, t in enumerate(items) if is_framework_noise(t)]]
    if not clean:
        return [], len(noisy)
    texts = [t for _, t in clean]
    embeddings = get_embeddings(texts)
    if embeddings is None:
        # Fallback: string dedup
        seen = []
        result = []
        for text in texts:
            if not any(similar(text, s) >= SIM_THRESHOLD for s in seen):
                result.append(text)
                seen.append(text)
        return result, len(items) - len(result)
    clusters = cluster_items(texts, embeddings)
    result = [merge_cluster([texts[i] for i in cluster]) for cluster in clusters]
    removed = len(items) - len(result)
    return result, removed

def extract_all_items(md_files):
    """
    Extract all tagged items across all memory files.
    Returns list of (file_path, line_index, raw_line, item_text).
    """
    all_items = []
    item_pattern = re.compile(r'^\s*-\s*(' + ALL_ITEM_TAGS + r')\s*(.+?)$')
    for md_file in md_files:
        lines = md_file.read_text(encoding='utf-8').split('\n')
        for i, line in enumerate(lines):
            m = item_pattern.match(line)
            if m:
                all_items.append((md_file, i, line, line.strip()))
    return all_items

def cleanup():
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_display = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

    md_files = sorted([f for f in MEMORY_DIR.glob("*.md") if f.name != "MEMORY.md" and not f.name.startswith("_")])
    removed_count = 0
    bootcheck_removed = 0

    # ── Pass 1: Semantic dedup across ALL files ──
    all_items = extract_all_items(md_files)
    if all_items:
        texts = [item[3] for item in all_items]
        deduped_texts, sem_removed = semantic_dedup_items(texts)
        deduped_set = set(deduped_texts)
        # Mark which lines to remove
        lines_to_remove = {}  # file_path -> set of line indices
        for (md_file, line_idx, raw_line, item_text) in all_items:
            if item_text not in deduped_set:
                lines_to_remove.setdefault(md_file, set()).add(line_idx)
        # Apply removals
        for md_file, remove_indices in lines_to_remove.items():
            lines = md_file.read_text(encoding='utf-8').split('\n')
            removed_lines = [lines[i] for i in remove_indices]
            kept_lines = [line for i, line in enumerate(lines) if i not in remove_indices]
            md_file.write_text('\n'.join(kept_lines), encoding='utf-8')
            with open(ARCHIVE_DIR / f"{md_file.stem}_dedup_{today_str}.md", 'a', encoding='utf-8') as f:
                f.write(f"# Semantic dedup removed from {md_file.name} on {today_display}\n")
                f.write('\n'.join(removed_lines) + '\n')
            removed_count += len(remove_indices)
        print(f"Semantic dedup: {removed_count} duplicates removed across {len(md_files)} files (TEI {'available' if get_embeddings(['test']) is not None else 'unavailable, used string fallback'}).")

    # ── Pass 2: TTL expiry cleanup ──
    today = datetime.now(timezone.utc).date()
    expires_pattern = re.compile(r'\[expires:(\d{4}-\d{2}-\d{2})\]')
    ttl_removed = 0
    for md_file in md_files:
        if not md_file.exists():
            continue
        lines = md_file.read_text(encoding='utf-8').split('\n')
        new_lines = []
        for line in lines:
            m = expires_pattern.search(line)
            if m:
                expiry = datetime.strptime(m.group(1), '%Y-%m-%d').date()
                if expiry < today:
                    ttl_removed += 1
                    continue
            new_lines.append(line)
        md_file.write_text('\n'.join(new_lines), encoding='utf-8')
    print(f"TTL expiry: {ttl_removed} expired items deleted.")

    # ── Pass 3: Bootcheck cleanup ──
    for md_file in md_files:
        if not md_file.exists():
            continue
        content = md_file.read_text(encoding='utf-8')
        lines = content.split('\n')
        has_real_content = bool(re.search(ALL_ITEM_TAGS, content))
        if not has_real_content:
            bootcheck_keywords = ['bootcheck', 'framework validation', 'operational procedures',
                                  'workflow_market', 'analysis_template', 'AI successfully recalled']
            if any(kw in content.lower() for kw in bootcheck_keywords):
                with open(ARCHIVE_DIR / f"{md_file.stem}_bootcheck_{today_str}.md", 'a', encoding='utf-8') as f:
                    f.write(f"# Bootcheck removal from {md_file.name} on {today_display}\n\n")
                    f.write(content + '\n')
                md_file.unlink()
                bootcheck_removed += 1

    print(f"Bootcheck cleanup: {bootcheck_removed} files removed.")

    trim_memory_index()


def trim_memory_index():
    """
    Trim MEMORY.md index if over MAX_INDEX_CHARS.
    Removes oldest entries (top of index) until under limit.
    Raw data in memory/*.md is never touched — index can be rebuilt anytime.
    """
    if not MEMORY_INDEX.exists():
        return

    content = MEMORY_INDEX.read_text(encoding="utf-8")
    if len(content) <= MAX_INDEX_CHARS:
        print(f"MEMORY.md: {len(content)} chars — under limit, no trim needed.")
        return

    print(f"MEMORY.md: {len(content)} chars exceeds limit ({MAX_INDEX_CHARS}) — trimming oldest entries...")

    lines = content.splitlines()

    # Find where index entries start (lines starting with [TAG])
    header_end = 0
    for i, line in enumerate(lines):
        if re.match(r'^\[[\w]+\]', line.strip()):
            header_end = i
            break

    header = lines[:header_end]
    entries = lines[header_end:]

    removed = 0
    while entries and len("\n".join(header + entries)) > MAX_INDEX_CHARS:
        entries.pop(0)
        removed += 1

    MEMORY_INDEX.write_text("\n".join(header + entries), encoding="utf-8")
    print(f"Trimmed {removed} oldest entries from MEMORY.md. Raw data in memory/*.md untouched.")
    print(f"To rebuild full index: python3 procedures/extract_memory.py --rebuild")


if __name__ == "__main__":
    cleanup()
