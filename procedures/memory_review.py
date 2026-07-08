#!/usr/bin/env python3
"""
memory_review.py — Full review of memory files with LLM.

No marking. No flags. Actual review.

Age is filename-based (YYYY-MM-DD.md), not mtime. This is intentional because
memory files are named by the date they were created, and mtime changes on edits.

For each file at review age:
1. Read file content
2. Send to LLM with current date
3. LLM classifies each entry: valid | invalidated | uncertain | noise
4. Rewrite file with reviewed entries (noise removed)
5. Delete file if entirely redundant

Output format: machine-readable tagged lines compatible with OpenClaw memory indexing.

Age buckets (full review triggers):
- 7 days:   Full review
- 14 days:  Full review
- 30 days:  Full review
- 60 days:  Full review
- 120 days: Full review

Deletion at 180 days is handled by cleanup_old_data.py — NOT this script.

Deduplication: .review_tracker.json prevents re-reviewing at same bucket.

BATCHED REVIEW (scale guard):
To handle large memory collections without overwhelming LLM context, review runs
in daily batches. Each run processes BATCH_SIZE files, rotating via cursor in
.review_cursor.json. Full cycle = ceil(total_files / BATCH_SIZE) days.
BATCH_SIZE is adaptive: scales with total file count so full cycle stays ~7 days.
"""

import json
import math
import os
import subprocess
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Workspace resolution (priority): DINOMEM_WORKSPACE env var > install-time sed
# substitution of DINOMEM_WORKSPACE_PLACEHOLDER > self-locate from this file's
# location (procedures/ is one level under the workspace root). The self-locate
# fallback keeps the script working if the install-time sed was skipped/failed
# (manual copy, partial install, moved workspace dir).
_WS_DEFAULT = "DINOMEM_WORKSPACE_PLACEHOLDER"
if _WS_DEFAULT.startswith("DINOMEM_"):  # sed did not run
    _WS_DEFAULT = str(Path(__file__).resolve().parent.parent)
WORKSPACE = Path(os.environ.get("DINOMEM_WORKSPACE", _WS_DEFAULT))
MEMORY_DIR = WORKSPACE / "memory"
REVIEW_TRACKER = MEMORY_DIR / ".review_tracker.json"
REVIEW_CURSOR = MEMORY_DIR / ".review_cursor.json"
REPORT_LOG = MEMORY_DIR / ".review_reports.log"

# Batched review config
# Adaptive batch size: ceil(total_files / 7) so full cycle ~= 7 days
# Minimum 5, maximum 50 per run to bound LLM calls
BATCH_MIN = 5
BATCH_MAX = 150
CYCLE_DAYS = 7

AGE_BUCKETS = [
    (7, "review"),
    (14, "review"),
    (30, "review"),
    (60, "review"),
    (120, "review"),
]

# Terminal review bucket. Surviving this bucket with an all-[valid] verdict
# graduates a file to FROZEN (immortal, no more scheduled review). This bounds
# per-entry review cost to its first ~120 days regardless of archive size.
TERMINAL_BUCKET = 120

# Inline frozen marker. Lives as the first line of the file (self-contained,
# survives file moves, greppable by the deterministic deleter — no sidecar
# desync risk). A frozen file is skipped by scheduled review forever; only
# semantic dedup / manual edits can still touch it (frozen != _pin_).
FROZEN_MARKER = "<!-- frozen: true -->"

# Per-entry verdict tag prefixes the review writes at line start.
VERDICT_TAGS = ("[valid]", "[invalidated]", "[uncertain]", "[noise]")


def is_frozen(filepath):
    """A file is frozen if its first non-empty line is the FROZEN_MARKER."""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s:
                    continue
                return s == FROZEN_MARKER
    except Exception:
        return False
    return False


def all_entries_valid(reviewed_text):
    """True if every verdict-tagged entry in the reviewed output is [valid].

    Only lines that carry a verdict tag are considered (blank lines, headers,
    and untagged prose are ignored). Returns False if there are zero tagged
    entries (nothing to graduate on)."""
    saw_tagged = False
    for raw in reviewed_text.splitlines():
        s = raw.strip().lstrip("-").strip()
        for tag in VERDICT_TAGS:
            if s.startswith(tag):
                saw_tagged = True
                if tag != "[valid]":
                    return False
                break
    return saw_tagged


def load_tracker():
    if REVIEW_TRACKER.exists():
        with open(REVIEW_TRACKER, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_tracker(tracker):
    with open(REVIEW_TRACKER, "w", encoding="utf-8") as f:
        json.dump(tracker, f, indent=2)

def load_cursor():
    if REVIEW_CURSOR.exists():
        try:
            with open(REVIEW_CURSOR, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"offset": 0, "total": 0, "cycle": 0}

def save_cursor(cursor):
    with open(REVIEW_CURSOR, "w", encoding="utf-8") as f:
        json.dump(cursor, f, indent=2)

def get_batch_size(total_files):
    """Adaptive batch size: ceil(total / CYCLE_DAYS), clamped to [BATCH_MIN, BATCH_MAX]."""
    import math
    if total_files == 0:
        return BATCH_MIN
    size = math.ceil(total_files / CYCLE_DAYS)
    return max(BATCH_MIN, min(BATCH_MAX, size))


def get_file_age(filepath):
    """Age is based on filename date (YYYY-MM-DD), not mtime."""
    file_date = filepath.stem
    try:
        file_dt = datetime.strptime(file_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        today = datetime.now(timezone.utc)
        return (today - file_dt).days
    except ValueError:
        return None


def _find_openclaw_bin():
    """Find openclaw binary: PATH first, then common install locations."""
    import shutil
    found = shutil.which("openclaw")
    if found:
        return found
    candidates = [
        "/home/linuxbrew/.linuxbrew/bin/openclaw",
        "/usr/local/bin/openclaw",
        "/usr/bin/openclaw",
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    return "openclaw"  # fallback, let subprocess raise if missing

def call_llm(prompt, max_tokens=4000):
    """Call LLM via OpenClaw gateway."""
    try:
        result = subprocess.run(
            [
                _find_openclaw_bin(),
                "capability", "model", "run",
                "--prompt", prompt,
                "--gateway",
                "--json",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0:
            output = json.loads(result.stdout)
            if output.get("ok") and output.get("outputs"):
                return output["outputs"][0].get("text", "")
    except Exception:
        pass
    return None


def review_file_with_llm(filepath, age):
    """Send file to LLM for full review. Returns reviewed content or None if failed."""
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    if not content.strip():
        return None

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    file_date = filepath.stem

    prompt = f"""You are reviewing a memory file for an AI agent.

File date: {file_date}
Review date: {today}
File age: {age} days

Memory file content:
---
{content}
---

Instructions:
1. For each entry, classify it:
   - [valid] — still accurate and useful. Keep the original text.
   - [invalidated] — wrong, expired, or the opposite happened. Keep the original text and add a brief outcome note.
   - [uncertain] — cannot verify. Keep as [uncertain] with original text.
   - [noise] — low-signal, vague, no actionable insight, or just chitchat. REMOVE it (do not include in output).

2. Be AGGRESSIVE about noise removal. If an entry is not useful to this agent, remove it.
   Examples of noise: generic commentary, vague predictions without specifics, restatements of known facts without new insight, social chat, "interesting" observations that don't affect decisions.

3. If ALL entries are redundant or noise, return exactly: ALL_REDUNDANT

4. Return ONLY the reviewed entries. One per line. Format:
   - [status] original text

5. For invalidated entries: briefly note why — what changed, what actually happened, or why it's no longer true.

6. For expired time-sensitive entries: note the expiry and what actually happened if known.

7. For framework/workflow facts that appear in newer files: they are redundant. Omit them.

8. Be concise. Do not add commentary outside the tagged lines.

9. Preserve the original text as much as possible. Only change the tag and add a brief outcome note for invalidated entries."""

    return call_llm(prompt, max_tokens=4000)


# ── Embedding pre-filter ─────────────────────────────────────────────────────
# Override with DINOMEM_EMBED_URL for a remote / non-Docker TEI-compatible server.
TEI_URL = os.environ.get("DINOMEM_EMBED_URL", "http://localhost:8080/v1/embeddings")
# Cosine similarity threshold — files above this are "similar enough" to need conflict check
PREFILTER_SIM_THRESHOLD = 0.82

def get_embeddings_for_files(filepaths):
    """
    Get TEI embeddings for a list of files.
    Returns dict {filepath: vector} or None if TEI unavailable.
    """
    if not filepaths:
        return {}
    texts = []
    valid_paths = []
    for fp in filepaths:
        try:
            content = fp.read_text(encoding="utf-8", errors="replace").strip()
            if content:
                texts.append(content[:1000])  # cap per-file to avoid huge payloads
                valid_paths.append(fp)
        except Exception:
            continue
    if not texts:
        return {}
    # TEI has a per-request payload/batch limit; sending all files at once can
    # return HTTP 413. Chunk into small batches and merge. If any batch fails,
    # treat TEI as unavailable (return None) so the caller falls back cleanly.
    EMBED_BATCH = 8
    vectors = []
    try:
        for i in range(0, len(texts), EMBED_BATCH):
            chunk = texts[i:i + EMBED_BATCH]
            payload = json.dumps({"input": chunk, "model": ""}).encode()
            req = urllib.request.Request(
                TEI_URL, data=payload,
                headers={"Content-Type": "application/json"}, method="POST"
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
            vectors.extend(item["embedding"] for item in data["data"])
        if len(vectors) != len(valid_paths):
            return None  # mismatch — treat as unavailable
        return dict(zip(valid_paths, vectors))
    except Exception:
        return None  # TEI unavailable

def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0

def prefilter_batch(filepaths):
    """
    Given a batch of files, return two lists:
    - priority: files that have at least one similar neighbor (need conflict/redundancy check)
    - isolated: files with no similar neighbors (safe to review without pre-check)

    If TEI unavailable, returns (filepaths, []) — all files treated as priority.
    """
    embeddings = get_embeddings_for_files(filepaths)
    if embeddings is None:
        # TEI unavailable — skip pre-filter, treat all as priority
        return list(filepaths), []

    paths = list(embeddings.keys())
    vectors = [embeddings[p] for p in paths]
    has_neighbor = [False] * len(paths)

    for i in range(len(paths)):
        for j in range(i + 1, len(paths)):
            if cosine(vectors[i], vectors[j]) >= PREFILTER_SIM_THRESHOLD:
                has_neighbor[i] = True
                has_neighbor[j] = True

    priority = [paths[i] for i in range(len(paths)) if has_neighbor[i]]
    isolated = [paths[i] for i in range(len(paths)) if not has_neighbor[i]]
    return priority, isolated


def review():
    today_display = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    tracker = load_tracker()
    cursor = load_cursor()

    all_md_files = sorted([f for f in MEMORY_DIR.glob("*.md") if f.name != "MEMORY.md" and not f.name.startswith("_")])
    total = len(all_md_files)
    batch_size = get_batch_size(total)

    # Rotate cursor: if total changed significantly, reset offset
    if cursor["total"] != total:
        cursor["offset"] = 0
        cursor["total"] = total

    offset = cursor["offset"]
    # Wrap around if offset exceeds total
    if offset >= total:
        offset = 0
        cursor["cycle"] = cursor.get("cycle", 0) + 1

    md_files = all_md_files[offset:offset + batch_size]
    cursor["offset"] = offset + len(md_files)
    save_cursor(cursor)

    print(f"Batched review: {len(md_files)}/{total} files (offset {offset}, batch_size {batch_size}, cycle {cursor['cycle']})")

    # Embedding pre-filter: prioritize files with similar neighbors
    priority_files, isolated_files = prefilter_batch(md_files)
    tei_active = not (priority_files == md_files and not isolated_files)
    if tei_active:
        print(f"Pre-filter: {len(priority_files)} priority (similar neighbors), {len(isolated_files)} isolated")
    else:
        print("Pre-filter: TEI unavailable, reviewing all files")

    # Review priority files first (conflict/redundancy candidates), then isolated
    ordered_files = priority_files + isolated_files

    files_reviewed = 0
    files_redundant = 0
    files_failed = 0
    all_changes = []

    files_frozen = 0

    for filepath in ordered_files:
        age = get_file_age(filepath)
        if age is None:
            continue

        # FROZEN files graduated out of scheduled review — skip forever.
        if is_frozen(filepath):
            continue

        file_key = str(filepath)

        # Find highest applicable bucket
        applicable_bucket = None
        applicable_action = None
        for days, action in AGE_BUCKETS:
            if age >= days:
                applicable_bucket = days
                applicable_action = action

        if applicable_bucket is None:
            continue

        # Deduplication: skip if already reviewed at this bucket or higher
        if file_key in tracker:
            last_reviewed = tracker[file_key]
            if last_reviewed >= applicable_bucket:
                continue

        # Full review with LLM
        llm_output = review_file_with_llm(filepath, age)

        if llm_output is None:
            files_failed += 1
            all_changes.append(f"FAILED: {filepath.name} (LLM call failed)")
            # Don't record in tracker — retry next run
            continue

        if llm_output.strip() == "ALL_REDUNDANT":
            try:
                filepath.unlink()
                files_redundant += 1
                all_changes.append(f"REDUNDANT: {filepath.name} (all entries noise/redundant)")
            except Exception as e:
                all_changes.append(f"REDUNDANT_ERROR: {filepath.name}: {e}")
            if file_key in tracker:
                del tracker[file_key]
            save_tracker(tracker)
            continue

        # GRADUATION: at the terminal bucket, if the file survived with an
        # all-[valid] verdict, freeze it (prepend the inline marker). Frozen
        # files are immortal to scheduled review; the deterministic deleter
        # (memory_retention.py) also honors the marker and never age-deletes
        # them. Cost is thus bounded to each entry's first ~120 days.
        graduate = (
            applicable_bucket >= TERMINAL_BUCKET
            and all_entries_valid(llm_output)
        )

        # Write reviewed content
        try:
            body = llm_output.strip() + "\n"
            if graduate:
                body = FROZEN_MARKER + "\n" + body
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(body)
            files_reviewed += 1
            tracker[file_key] = applicable_bucket
            save_tracker(tracker)
            if graduate:
                files_frozen += 1
                all_changes.append(f"FROZEN: {filepath.name} (age {age}d, all [valid] @ {applicable_bucket}d — graduated, immortal)")
            else:
                all_changes.append(f"REVIEWED: {filepath.name} (age {age}d)")
        except Exception as e:
            all_changes.append(f"WRITE_ERROR: {filepath.name}: {e}")

    save_tracker(tracker)

    summary = f"""
=== MEMORY REVIEW REPORT ({today_display}) ===

Files reviewed: {files_reviewed}
Files frozen (graduated): {files_frozen}
Files redundant/noise: {files_redundant}
Files failed: {files_failed}

Changes:
"""
    for change in all_changes:
        summary += f"  {change}\n"

    print(summary)

    with open(REPORT_LOG, "a", encoding="utf-8") as f:
        f.write(summary + "\n")

    return summary


if __name__ == "__main__":
    review()
