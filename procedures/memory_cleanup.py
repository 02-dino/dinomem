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
import os
import re
import time
import urllib.request
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

# Workspace resolution (priority): DINOMEM_WORKSPACE env var > install-time sed
# substitution of DINOMEM_WORKSPACE_PLACEHOLDER > self-locate from this file's
# location. Self-locate fallback keeps the script working if install-time sed
# was skipped/failed (manual copy, partial install, moved workspace dir).
_WS_DEFAULT = "DINOMEM_WORKSPACE_PLACEHOLDER"
if _WS_DEFAULT.startswith("DINOMEM_"):  # sed did not run
    _WS_DEFAULT = str(Path(__file__).resolve().parent.parent)
WORKSPACE = Path(os.environ.get("DINOMEM_WORKSPACE", _WS_DEFAULT))
MEMORY_DIR = WORKSPACE / "memory"
ARCHIVE_DIR = WORKSPACE / ".memory_archive"
MEMORY_INDEX = WORKSPACE / "MEMORY.md"

# Retention for .memory_archive/: pre-dedup snapshots are continuity-only (rollback +
# audit), NOT live-indexed by memory_search. Prune files older than this so the
# archive can't grow unbounded across the install's lifetime. Plain markdown, tiny
# volume, but a hard cap keeps every install clean without manual intervention.
ARCHIVE_RETENTION_DAYS = 180

# Sessions dir for recency section
OPENCLAW_ROOT = WORKSPACE.parent
AGENT_ID = "DINOMEM_AGENT_ID_PLACEHOLDER"
SESSIONS_DIR = OPENCLAW_ROOT / "agents" / AGENT_ID / "sessions"
RECENCY_MARKER_START = "<!-- dinomem:recency-start -->"
RECENCY_MARKER_END = "<!-- dinomem:recency-end -->"
OPENPROJ_MARKER_START = "<!-- dinomem:open-projects-start -->"
OPENPROJ_MARKER_END = "<!-- dinomem:open-projects-end -->"

SIM_THRESHOLD = 0.80          # string similarity fallback
SEMANTIC_THRESHOLD = 0.88     # cosine similarity for semantic dedup

def _resolve_max_index_chars(default_cap: int = 20000, floor: int = 18000) -> int:
    """Trim MEMORY.md at 90% of the LIVE bootstrap per-file cap, not a stale
    hardcode. install.sh raises agents.defaults.bootstrapMaxChars to fit the
    injected blocks (+10k headroom); if this trimmer kept cutting at a fixed
    18000 it would shrink MEMORY.md back below the raised cap and silently waste
    all the headroom install.sh just bought. So read the cap from openclaw.json
    (same config-read pattern as extract_memory.py's auto-routing) and track it.

    Returns 90% of the resolved cap (keep a 10% margin so the index never rides
    the exact truncation edge). Floors at 18000 so we never trim MORE aggressively
    than the original behavior, even if the cap is read as a small/absent value.
    """
    try:
        cfg_path = Path.home() / ".openclaw" / "openclaw.json"
        if not cfg_path.exists():
            return floor
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        defaults = cfg.get("agents", {}).get("defaults", {})
        cap = defaults.get("bootstrapMaxChars", default_cap)
        if not isinstance(cap, (int, float)) or cap <= 0:
            cap = default_cap
        return max(floor, int(cap * 0.9))
    except Exception:
        return floor

MAX_INDEX_CHARS = _resolve_max_index_chars()  # 90% of LIVE bootstrapMaxChars (>=18000 floor)
TEI_URL = "http://localhost:8080/v1/embeddings"
ALL_ITEM_TAGS = r'\[factual\]|\[pattern\]|\[operational\]|\[decision\]|\[correction\]|\[preference\]|\[uncertain\]|\[lesson\]|\[prediction\]'
# Bare daily files (memory/YYYY-MM-DD.md) are written by OpenClaw memoryFlush
# solely to feed startupContext. They are flush-owned, untagged prose, and
# pruned by cleanup_startup_daily.py. dinomem's dedup/TTL/bootcheck passes must
# NOT touch them (bootcheck especially would delete an untagged file that
# happens to mention a framework keyword). Skip them everywhere.
BARE_DAILY_RE = re.compile(r'^\d{4}-\d{2}-\d{2}\.md$')

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

# Frozen marker (must match memory_review.py / memory_retention.py). A frozen
# file has graduated (all entries [valid] survived every review bucket) and is
# immortal — semantic dedup MUST NOT drop its entries in favor of a lower-value
# twin. We exclude frozen files from dedup entirely (self-contained + safe).
FROZEN_MARKER = "<!-- frozen: true -->"

def is_frozen(filepath):
    """Frozen if the first non-empty line is the frozen marker."""
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

def extract_all_items(md_files):
    """
    Extract all tagged items across all memory files.
    Returns list of (file_path, line_index, raw_line, item_text).

    Frozen files are skipped: their entries are immortal keepers and must never
    be dropped as a near-twin of a non-frozen (lower-value) item.
    """
    all_items = []
    item_pattern = re.compile(r'^\s*-\s*(' + ALL_ITEM_TAGS + r')\s*(.+?)$')
    for md_file in md_files:
        if is_frozen(md_file):
            continue
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

    md_files = sorted([f for f in MEMORY_DIR.glob("*.md")
                       if f.name != "MEMORY.md"
                       and not f.name.startswith("_")
                       and not BARE_DAILY_RE.match(f.name)])
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

    pruned = prune_archive()
    print(f"Archive retention: {pruned} file(s) older than {ARCHIVE_RETENTION_DAYS}d pruned from .memory_archive/.")

    # MEMORY.md writer ownership. In base-only installs, memory_cleanup is the
    # sole writer of the MEMORY.md navigation index (recency / open-projects /
    # trim). When the neuron layer is installed, generate_topic_index.py becomes
    # the authoritative MEMORY.md writer (LLM-enriched index + cap + line guard).
    # If both wrote the file we'd get two writers racing on the same file. So we
    # auto-yield: if neuron is present, skip the MEMORY.md-writing steps here and
    # let generate_topic_index own the file. Dedup / bootcheck / archive-prune
    # above still run either way. Override with DINOMEM_FORCE_INDEX_WRITER=1.
    neuron_present = (WORKSPACE / "procedures" / "generate_topic_index.py").exists()
    if neuron_present and os.environ.get("DINOMEM_FORCE_INDEX_WRITER") != "1":
        print("neuron detected (generate_topic_index.py present) — yielding MEMORY.md writing to neuron; skipping recency/open-projects/trim here.")
        return

    trim_memory_index()
    update_recency_section()
    update_open_projects_section()


def prune_archive():
    """Delete .memory_archive/ files older than ARCHIVE_RETENTION_DAYS (by mtime).
    Continuity-only snapshots; safe to GC once past the retention window."""
    if not ARCHIVE_DIR.exists():
        return 0
    cutoff = time.time() - ARCHIVE_RETENTION_DAYS * 86400
    pruned = 0
    for f in ARCHIVE_DIR.glob("*.md"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                pruned += 1
        except OSError:
            continue
    return pruned


def get_recent_flush_context():
    """
    Read daily memory flush files (today + yesterday) and extract structured
    recent context: named repos/projects, key decisions, recent commits.
    Returns formatted section string or None.
    """
    from datetime import timedelta
    try:
        today = datetime.now().strftime('%Y-%m-%d')
        yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
        lines_out = []

        for date_str in [today, yesterday]:
            flush_file = MEMORY_DIR / f'{date_str}.md'
            if not flush_file.exists():
                continue
            text = flush_file.read_text(encoding='utf-8')
            if not text.strip():
                continue

            # Extract key decisions
            decisions = []
            in_decisions = False
            for line in text.splitlines():
                if re.match(r'^#{1,3}\s+Key decisions', line, re.IGNORECASE):
                    in_decisions = True
                    continue
                if in_decisions:
                    if re.match(r'^#{1,3}\s+', line):
                        in_decisions = False
                        continue
                    m = re.match(r'^[-*]\s+(.+)', line)
                    if m:
                        decisions.append(m.group(1).strip())

            # Extract named repos from section headers + commit/push lines + paths
            paths = []
            named_repos = []
            for line in text.splitlines():
                m = re.match(r'^#{1,4}\s+.*(committed to|pushed to|built in|changes in|repo:|project:)\s*([\w\-\.]+)', line, re.IGNORECASE)
                if m:
                    repo = m.group(2).strip()
                    if repo and repo not in named_repos:
                        named_repos.append(f'{repo} (repo/project from session header)')
                m = re.search(r'[Cc]ommitted?\s+`?([a-f0-9]{6,8})`?', line)
                if m:
                    clean = re.sub(r'[*`]', '', line).strip().lstrip('-').strip()
                    if clean and clean not in paths:
                        paths.append(clean[:120])
                        continue
                if re.search(r'[Pp]ushed', line) and ('github' in line.lower() or 'repo' in line.lower() or '.git' in line.lower()):
                    clean = re.sub(r'[*`]', '', line).strip().lstrip('-').strip()
                    if clean and clean not in paths:
                        paths.append(clean[:120])
                        continue
                m = re.search(r'(/root/[\w/.\-]+|github/[\w/.\-]+|workspace-[\w]+/[\w/.\-]+)', line)
                if m and m.group(1) not in paths:
                    paths.append(m.group(1))

            # No hard cap — trim_memory_index() handles MEMORY.md size.
            # Recent Context is at top so it survives trimming (recency wins).
            paths = list(dict.fromkeys(named_repos + paths))
            decisions = list(dict.fromkeys(decisions))

            if paths or decisions:
                lines_out.append(f'### {date_str}')
                if paths:
                    lines_out.append('**Recent work:**')
                    for p in paths:
                        lines_out.append(f'- {p}')
                if decisions:
                    lines_out.append('**Key decisions:**')
                    for d in decisions:
                        lines_out.append(f'- {d}')

        if not lines_out:
            return None

        body = '\n'.join(lines_out)
        return (f'{RECENCY_MARKER_START}\n'
                f'## Recent Context (from memory flush \u2014 no search needed)\n'
                f'{body}\n'
                f'{RECENCY_MARKER_END}')

    except Exception as e:
        print(f'\u26a0\ufe0f  get_recent_flush_context error: {e}')
        return None


def get_previous_session_topics():
    """
    Find the most recently archived session JSONL and extract topic hints.
    Returns formatted section string or None.
    """
    if not SESSIONS_DIR.exists():
        return None
    try:
        archived = sorted(
            SESSIONS_DIR.glob('*.archived.reset.*.jsonl'),
            key=lambda f: f.stat().st_mtime,
            reverse=True
        )
        if not archived:
            return None

        latest = archived[0]
        try:
            ts_part = latest.stem.split('.archived.reset.')[-1]
            session_date = ts_part[:10]
        except Exception:
            session_date = datetime.fromtimestamp(latest.stat().st_mtime).strftime('%Y-%m-%d')

        topics = []

        # Pull slugs from memory files dated on session_date
        for md_file in MEMORY_DIR.glob('*.md'):
            if md_file.name.startswith('_') or md_file.name == 'MEMORY.md':
                continue
            if session_date in md_file.name:
                parts = md_file.stem.split('_', 2)
                if len(parts) >= 3:
                    slug = parts[2].replace('-', ' ')
                    if slug not in topics:
                        topics.append(slug)

        # Fallback: extract keywords from user messages in archived JSONL
        if not topics:
            user_messages = []
            try:
                with open(latest, 'r', encoding='utf-8', errors='replace') as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                            msg = entry.get('message', entry)
                            role = msg.get('role') or entry.get('role')
                            if role == 'user':
                                content = msg.get('content', '')
                                if isinstance(content, list):
                                    content = ' '.join(b.get('text', '') for b in content if isinstance(b, dict))
                                if isinstance(content, str) and len(content) > 10:
                                    user_messages.append(content[:200])
                                if len(user_messages) >= 30:
                                    break
                        except Exception:
                            continue
            except Exception:
                pass

            if user_messages:
                stopwords = {'the','a','an','is','are','was','were','be','been','being',
                             'have','has','had','do','does','did','will','would','could',
                             'should','may','might','shall','can','to','of','in','for',
                             'on','with','at','by','from','up','about','this','that',
                             'these','those','i','you','he','she','it','we','they',
                             'what','which','who','how','why','when','where','and','or',
                             'but','if','then','than','so','not','no','just','very',
                             'also','too','only','even','already','still','now','here',
                             'kita','yang','di','ke','dari','dan','atau','ini','itu',
                             'ya','ga','gak','aja','juga','bisa','ada','mau','udah','kalau'}
                word_freq = {}
                for msg in user_messages:
                    words = re.findall(r'[a-zA-Z][a-zA-Z0-9_]{2,}', msg.lower())
                    for w in words:
                        if w not in stopwords and len(w) > 3:
                            word_freq[w] = word_freq.get(w, 0) + 1
                top_words = sorted(word_freq.items(), key=lambda x: x[1], reverse=True)[:8]
                topics = [w for w, _ in top_words]

        if not topics:
            return None

        # Cap the Previous Session keyword line. The slug path above has NO
        # per-item limit — a heavy work-day produces hundreds of dated memory
        # files -> hundreds of slugs -> one multi-KB line. trim_memory_index()
        # removes whole lines, never within a line, so an over-long single line
        # can survive trimming and bloat the injected MEMORY.md. Cap here: keep
        # at most _PREV_SESSION_MAX_TOPICS items AND hard-ceil the joined length.
        _PREV_SESSION_MAX_TOPICS = 15
        _PREV_SESSION_MAX_CHARS = 600
        topics = topics[:_PREV_SESSION_MAX_TOPICS]
        topics_str = ', '.join(topics)
        if len(topics_str) > _PREV_SESSION_MAX_CHARS:
            topics_str = topics_str[:_PREV_SESSION_MAX_CHARS].rsplit(', ', 1)[0] + ', …'
        return f'{RECENCY_MARKER_START}\n## Previous Session ({session_date}) — search if relevant\n{topics_str}\n{RECENCY_MARKER_END}'

    except Exception as e:
        print(f'⚠️  get_previous_session_topics error: {e}')
        return None

def get_open_projects_section():
    """
    Scan memory/_note_*.md for in_progress notes (project executor + paused tasks)
    and build an 'Open Projects' block for MEMORY.md so the agent sees unfinished
    work at session start without having to glob voluntarily.

    SCAN-BUT-EMIT-ONLY-IF-PRESENT: base dinomem notes are task_bound/time_bound
    (status pending|done, no in_progress) and have no project executor, so this
    returns None in a pure-base install (no-op). It only emits where in_progress
    notes exist (neuron project executor / configured agents). Single source of
    truth in base; neuron-only behaviour.
    """
    try:
        memory_dir = MEMORY_INDEX.parent / 'memory'
        if not memory_dir.exists():
            return None
        rows = []
        for note in sorted(memory_dir.glob('_note_*.md')):
            try:
                text = note.read_text(encoding='utf-8')
            except Exception:
                continue
            status = re.search(r'^status:\s*(.+)$', text, re.MULTILINE)
            if not status or status.group(1).strip() != 'in_progress':
                continue
            title = re.search(r'^#\s*(?:Project:\s*)?(.+)$', text, re.MULTILINE)
            name = title.group(1).strip() if title else note.stem.replace('_note_', '')
            step = re.search(r'^current_step:\s*(.+)$', text, re.MULTILINE)
            steps_total = len(re.findall(r'^\s*-\s*\[[ x]\]\s', text, re.MULTILINE))
            step_str = ''
            if step:
                cur = step.group(1).strip()
                step_str = f' — step {cur}' + (f'/{steps_total}' if steps_total else '')
            rows.append(f'- **{name}**{step_str} — resume from its `resume_state` (file: `{note.name}`)')
        if not rows:
            return None
        body = '\n'.join(rows)
        return (f'{OPENPROJ_MARKER_START}\n## Open Projects (resume these — read the note before answering)\n'
                f'{body}\n{OPENPROJ_MARKER_END}')
    except Exception as e:
        print(f'⚠️  get_open_projects_section error: {e}')
        return None

def update_open_projects_section():
    """Inject or update the Open Projects section in MEMORY.md (marker-bounded)."""
    if not MEMORY_INDEX.exists():
        return
    block = get_open_projects_section()
    content = MEMORY_INDEX.read_text(encoding='utf-8')
    if OPENPROJ_MARKER_START in content:
        start = content.index(OPENPROJ_MARKER_START)
        end = content.index(OPENPROJ_MARKER_END) + len(OPENPROJ_MARKER_END)
        content = content[:start].rstrip() + '\n' + content[end:].lstrip()
    if not block:
        MEMORY_INDEX.write_text(content, encoding='utf-8')
        return
    # Inject at very top, before recency/searchable, so it is the first thing seen
    lines = content.splitlines()
    insert_at = len(lines)
    for i, line in enumerate(lines):
        if line.startswith('## ') and i > 0:
            insert_at = i
            break
    lines.insert(insert_at, '')
    lines.insert(insert_at + 1, block)
    lines.insert(insert_at + 2, '')
    MEMORY_INDEX.write_text('\n'.join(lines), encoding='utf-8')
    print('Open Projects section updated in MEMORY.md.')

def update_recency_section():
    """
    Inject or update the recency section in MEMORY.md.
    Uses markers to replace existing section cleanly.
    """
    if not MEMORY_INDEX.exists():
        return

    recency = get_recent_flush_context() or get_previous_session_topics()
    content = MEMORY_INDEX.read_text(encoding='utf-8')

    # Remove existing recency block if present
    if RECENCY_MARKER_START in content:
        start = content.index(RECENCY_MARKER_START)
        end = content.index(RECENCY_MARKER_END) + len(RECENCY_MARKER_END)
        content = content[:start].rstrip() + '\n' + content[end:].lstrip()

    if not recency:
        MEMORY_INDEX.write_text(content, encoding='utf-8')
        return

    # Inject after first header line (## Searchable or first ## section)
    lines = content.splitlines()
    insert_at = len(lines)  # default: append
    for i, line in enumerate(lines):
        if line.startswith('## Searchable') or (line.startswith('## ') and i > 0):
            insert_at = i
            break

    lines.insert(insert_at, '')
    lines.insert(insert_at + 1, recency)
    lines.insert(insert_at + 2, '')
    MEMORY_INDEX.write_text('\n'.join(lines), encoding='utf-8')
    print(f'Recency section updated in MEMORY.md ({"ok" if recency else "none"}).')

def trim_memory_index():
    """
    Trim MEMORY.md index if over MAX_INDEX_CHARS.
    Removes oldest entries (top of index) until under limit.
    Raw data in memory/*.md is never touched — index can be rebuilt anytime.
    """
    if not MEMORY_INDEX.exists():
        return

    content = MEMORY_INDEX.read_text(encoding="utf-8")

    # Safety net: before anything, collapse any single over-long line in place.
    # trim_memory_index removes whole lines and only touches [TAG] entries, so a
    # blown-up non-[TAG] line (e.g. the Previous Session keyword blob) could
    # otherwise survive trimming and bloat the injected file. Truncate on a word
    # boundary. Mirrors neuron generate_topic_index._guard_memory_size fix B.
    _MAX_SINGLE_LINE_CHARS = 800
    if any(len(ln) > _MAX_SINGLE_LINE_CHARS for ln in content.splitlines()):
        fixed = []
        for ln in content.splitlines():
            if len(ln) > _MAX_SINGLE_LINE_CHARS:
                ln = ln[:_MAX_SINGLE_LINE_CHARS].rsplit(' ', 1)[0] + ' …'
            fixed.append(ln)
        content = '\n'.join(fixed)
        MEMORY_INDEX.write_text(content, encoding="utf-8")
        print(f"MEMORY.md had an over-long line (> {_MAX_SINGLE_LINE_CHARS} chars) — collapsed in place.")

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
