#!/usr/bin/env python3
"""
Session Reset Script

Resets analysis agent sessions:
  - Archives orphaned session files (>48h old)
  - Deletes old archives (>5 days)
  - Resets tracked sessions (chat >7 days, cron >1 day, compaction >5)
  - Cleans JSONL content before archiving

Run via orchestrator (auto_session_reset.py) or standalone.
Logs to: logs/session_reset.log
"""

import json
import re
from pathlib import Path
from datetime import datetime, timedelta

# ─── Configuration ────────────────────────────────────────────────────────────

ARCHIVE_MAX_AGE_DAYS = 7
ORPHAN_MAX_AGE_HOURS = 48
MIN_MESSAGE_LENGTH = 15
MAX_SESSION_AGE_DAYS = 7
MAX_SESSION_AGE_DAYS_CRON = 1
COMPACTION_THRESHOLD = 2  # number of compaction generations (parentSession chain depth)
SESSIONS_DIR = Path("DINOMEM_AGENT_SESSIONS_PLACEHOLDER")
SESSIONS_FILE = SESSIONS_DIR / "sessions.json"
LOG_FILE = Path(__file__).parent.parent / "logs" / "session_reset.log"

# Ensure log dir exists
LOG_FILE.parent.mkdir(exist_ok=True)


# ═══════════════════════════════════════════════════════════════════════════════
# LOGGING & UTILITIES
# ═══════════════════════════════════════════════════════════════════════════════

def log(message):
    """Write to log file with timestamp"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_message = f"[{timestamp}] {message}\n"
    print(log_message.strip())
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(log_message)


def sanitize_text(text):
    """Remove invalid Unicode characters and surrogates"""
    if not isinstance(text, str):
        return ""
    return text.encode('utf-8', 'ignore').decode('utf-8')


def has_meaningful_content(text):
    """Check if text contains actual readable content"""
    if not text:
        return False
    return bool(re.search(r'[a-zA-Z0-9\u4e00-\u9fff]', text))


def extract_message_content(message):
    """Extract text content from a message object"""
    content = message.get('content', [])
    text = ""
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get('type') == 'text':
                text += item.get('text', '')
    elif isinstance(content, str):
        text = content
    return sanitize_text(text).strip()


def is_valid_message(data):
    """Check if a JSONL entry is a valid message to keep."""
    if data.get('type') == 'compaction':
        return True, "compaction summary"
    if data.get('type') != 'message':
        return False, "not a message"
    message = data.get('message', {})
    role = message.get('role')
    if role not in ['user', 'assistant']:
        return False, f"role={role}"
    text = extract_message_content(message)
    if len(text) <= MIN_MESSAGE_LENGTH:
        return False, f"too short ({len(text)} chars)"
    if not has_meaningful_content(text):
        return False, "no meaningful content"
    return True, "valid"


def cleanup_jsonl_content(file_path):
    """Read a JSONL file and return cleaned content. Preserves session header (type=session)."""
    cleaned_lines = []
    stats = {'total': 0, 'kept': 0, 'removed': 0, 'reasons': {}}
    try:
        with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                stats['total'] += 1
                try:
                    data = json.loads(line)
                    # Always preserve session header (contains parentSession for chain traversal)
                    if data.get('type') == 'session':
                        cleaned_lines.append(json.dumps(data, ensure_ascii=False, separators=(',', ':')))
                        stats['kept'] += 1
                        continue
                    is_valid, reason = is_valid_message(data)
                    if is_valid:
                        cleaned_lines.append(json.dumps(data, ensure_ascii=False, separators=(',', ':')))
                        stats['kept'] += 1
                    else:
                        stats['removed'] += 1
                        stats['reasons'][reason] = stats['reasons'].get(reason, 0) + 1
                except json.JSONDecodeError:
                    stats['removed'] += 1
                    stats['reasons']['json_error'] = stats['reasons'].get('json_error', 0) + 1
    except Exception as e:
        log(f"   ⚠️  Error reading {file_path.name}: {e}")
    return cleaned_lines, stats


def write_cleaned_jsonl(file_path, lines):
    """Write cleaned lines to a JSONL file"""
    if not lines:
        return False
    try:
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines) + '\n')
        return True
    except Exception as e:
        log(f"   ⚠️  Error writing {file_path.name}: {e}")
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# COMPACTION CHAIN TRACKING
# ═══════════════════════════════════════════════════════════════════════════════

def get_session_header(file_path):
    """Read the first non-empty line of a JSONL and return parsed header if type=session."""
    try:
        with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                if data.get('type') == 'session':
                    return data
                return None  # First non-empty line is not a session header
    except Exception:
        return None

def get_compaction_depth(session_file):
    """
    Traverse parentSession chain to count compaction generations.
    parentSession = full path to predecessor JSONL.
    Returns depth (0 = no compaction history).
    """
    depth = 0
    current = Path(session_file)
    seen = set()
    while True:
        if not current.exists():
            break
        resolved = str(current.resolve())
        if resolved in seen:
            break
        seen.add(resolved)
        header = get_session_header(current)
        if not header:
            break
        parent = header.get('parentSession')
        if not parent:
            break
        depth += 1
        current = Path(parent)
    return depth

def get_active_compaction_count(session_file):
    """
    Count compaction entries in JSONL (truncate=false mode).
    Falls back to parentSession chain depth (truncate=true mode).
    """
    count = 0
    try:
        with open(session_file, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    if data.get('type') == 'compaction':
                        count += 1
                except json.JSONDecodeError:
                    continue
    except Exception:
        pass
    # If no inline compaction entries, check chain depth (truncate=true mode)
    if count == 0:
        count = get_compaction_depth(session_file)
    return count

# ═══════════════════════════════════════════════════════════════════════════════
# SESSION FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

def get_all_sessions():
    """Get all sessions for analysis agent from sessions.json"""
    if not SESSIONS_FILE.exists():
        log("⚠️  sessions.json not found")
        return {}
    with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
        sessions = json.load(f)
    return {
        key: value for key, value in sessions.items()
        if key.startswith("agent:analyst:")
    }


def get_tracked_files():
    """Get all session files currently tracked in sessions.json"""
    if not SESSIONS_FILE.exists():
        return set()
    with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
        sessions = json.load(f)
    tracked = set()
    for key, data in sessions.items():
        if not key.startswith("agent:analyst:"):
            continue
        if "sessionFile" in data:
            tracked.add(Path(data["sessionFile"]).resolve())
        elif "sessionId" in data:
            tracked.add((SESSIONS_DIR / f"{data['sessionId']}.jsonl").resolve())
    return tracked


def get_orphaned_files():
    """Find session .jsonl files not tracked in sessions.json."""
    tracked = get_tracked_files()
    orphaned = []
    for file in SESSIONS_DIR.glob("*.jsonl"):
        name = file.name
        if any(marker in name for marker in ['.archived.', '.checkpoint.', '.reset.']):
            continue
        if file.resolve() not in tracked:
            try:
                mtime = datetime.fromtimestamp(file.stat().st_mtime)
                age_hours = (datetime.now() - mtime).total_seconds() / 3600
                orphaned.append((file, age_hours, mtime))
            except OSError:
                continue
    return orphaned


def archive_orphaned_file(file_path, age_hours=None):
    """Archive an orphaned session file with cleaned content."""
    if not file_path.exists():
        return False, None
    timestamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    archived_name = f"{file_path.stem}.archived.orphan.{timestamp}.jsonl"
    archived = SESSIONS_DIR / archived_name
    cleaned_lines, stats = cleanup_jsonl_content(file_path)
    if cleaned_lines:
        if write_cleaned_jsonl(archived, cleaned_lines):
            file_path.unlink()
            age_info = f" (age: {age_hours:.1f}h)" if age_hours else ""
            log(f"✅ Archived orphaned: {file_path.name} → {archived.name}{age_info}")
            log(f"   🧹 Cleaned: {stats['kept']} kept, {stats['removed']} removed")
            return True, {'file': archived_name, 'stats': stats, 'type': 'orphan'}
    else:
        file_path.unlink()
        log(f"🗑️  Deleted empty orphan: {file_path.name}")
        return True, {'file': file_path.name, 'stats': stats, 'type': 'orphan_deleted'}
    return False, None


def cleanup_orphaned_files():
    """Find and archive orphaned session files."""
    log("🔍 Checking for orphaned session files...")
    orphaned = get_orphaned_files()
    if not orphaned:
        log("✅ No orphaned files found")
        return 0, []
    archived_count = 0
    skipped_count = 0
    archive_info = []
    for file, age_hours, mtime in orphaned:
        if age_hours >= ORPHAN_MAX_AGE_HOURS:
            success, info = archive_orphaned_file(file, age_hours)
            if success and info:
                archived_count += 1
                archive_info.append(info)
        else:
            log(f"⏳ Skipped (too fresh: {age_hours:.1f}h < {ORPHAN_MAX_AGE_HOURS}h): {file.name}")
            skipped_count += 1
    if archived_count > 0:
        log(f"✅ Archived {archived_count} orphaned file(s)")
    if skipped_count > 0:
        log(f"⏳ Skipped {skipped_count} fresh orphaned file(s)")
    return archived_count, archive_info


def cleanup_old_archives():
    """Delete archived JSONL files older than ARCHIVE_MAX_AGE_DAYS."""
    log("🗑️  Checking for old archived files...")
    cutoff_time = datetime.now().timestamp() - (ARCHIVE_MAX_AGE_DAYS * 24 * 60 * 60)
    total_deleted = 0
    total_kept = 0
    deleted_files = []
    kept_files = []
    for file in SESSIONS_DIR.glob("*.archived.*.jsonl"):
        file_mtime = file.stat().st_mtime
        if file_mtime < cutoff_time:
            deleted_files.append(file.name)
            file.unlink()
            log(f"  🗑️  Deleted (older than {ARCHIVE_MAX_AGE_DAYS} days): {file.name}")
            total_deleted += 1
        else:
            kept_files.append(file.name)
            total_kept += 1
    if total_deleted > 0:
        log(f"✅ Cleanup complete! Deleted {total_deleted} old archive(s), kept {total_kept}")
    else:
        log(f"✅ No cleanup needed (all archives within {ARCHIVE_MAX_AGE_DAYS} days)")
    return {'deleted': deleted_files, 'kept': kept_files, 'deleted_count': total_deleted, 'kept_count': total_kept}


def filter_sessions_to_reset(sessions):
    """Filter sessions needing reset based on age/compaction."""
    sessions_to_reset = {}
    sessions_skipped = {}
    GRACE_PERIOD_MINUTES = 10
    now = datetime.now()
    for session_key, session_data in sessions.items():
        age_trigger = False
        compaction_trigger = False
        age_days = 0
        minutes_since_update = None
        is_cron = "cron:" in session_key
        max_age = MAX_SESSION_AGE_DAYS_CRON if is_cron else MAX_SESSION_AGE_DAYS
        updated_at = session_data.get("updatedAt")
        if updated_at:
            try:
                last_dt = datetime.fromtimestamp(updated_at / 1000)
                age_days = (now - last_dt).days
                minutes_since_update = (now - last_dt).total_seconds() / 60
                if age_days > max_age:
                    age_trigger = True
            except Exception:
                pass
        # Check compaction count from THREE sources, take the max (OR logic):
        #   1. JSONL inline type="compaction" entries (truncate=false mode)
        #   2. parentSession chain depth fallback (truncate=true mode) — both via get_active_compaction_count
        #   3. sessions.json "compactionCount" field — OpenClaw's authoritative runtime counter,
        #      survives JSONL cleanup/truncation/chain breaks. Defensive belt-and-suspenders:
        #      catches the case where (1)+(2) read 0 but OpenClaw knows compactions happened.
        session_id = session_data.get("sessionId", "")
        session_file_str = session_data.get("sessionFile")
        if session_file_str:
            session_file = Path(session_file_str)
        elif session_id:
            session_file = SESSIONS_DIR / f"{session_id}.jsonl"
        else:
            session_file = None
        jsonl_compaction_count = 0
        if session_file and session_file.exists():
            jsonl_compaction_count = get_active_compaction_count(session_file)
        sessions_json_compaction_count = session_data.get("compactionCount", 0) or 0
        compaction_count = max(jsonl_compaction_count, sessions_json_compaction_count)
        if compaction_count >= COMPACTION_THRESHOLD:
            compaction_trigger = True
        should_reset = age_trigger or compaction_trigger
        if should_reset and minutes_since_update is not None and minutes_since_update < GRACE_PERIOD_MINUTES:
            log(f"  ⏳ Skipping {session_key} - active conversation (updated {minutes_since_update:.1f} min ago)")
            should_reset = False
        if should_reset:
            sessions_to_reset[session_key] = session_data
        else:
            sessions_skipped[session_key] = {
                'age_days': age_days,
                'minutes_since_update': minutes_since_update,
                'compactions': compaction_count
            }
    return sessions_to_reset, sessions_skipped


def archive_predecessor_chain(session_file, timestamp):
    """
    With truncateAfterCompaction=true, predecessor JSONLs are left in place (untracked).
    Traverse the parentSession chain and archive each predecessor so
    extract_memory.py can process them without delay.
    """
    current = session_file
    archived_count = 0
    seen = set()
    while True:
        header = get_session_header(current)
        if not header:
            break
        parent_path_str = header.get('parentSession')
        if not parent_path_str:
            break
        parent = Path(parent_path_str)
        if not parent.exists():
            break
        resolved = str(parent.resolve())
        if resolved in seen:
            break
        seen.add(resolved)
        if '.archived.' not in parent.name:
            archived_name = f"{parent.stem}.archived.reset.{timestamp}.jsonl"
            archived = SESSIONS_DIR / archived_name
            cleaned_lines, stats = cleanup_jsonl_content(parent)
            if cleaned_lines:
                if write_cleaned_jsonl(archived, cleaned_lines):
                    parent.unlink()
                    log(f"   ✅ Archived predecessor: {parent.name} → {archived_name}")
                    log(f"      🧹 Cleaned: {stats['kept']} kept, {stats['removed']} removed")
                    archived_count += 1
                    current = archived
                else:
                    break
            else:
                parent.unlink()
                log(f"   🗑️  Deleted empty predecessor: {parent.name}")
                break
        else:
            current = parent
    return archived_count

def reset_session(session_key, session_data):
    """Reset session by archiving JSONL and deleting session mapping."""
    log(f"🔄 Starting session reset for: {session_key}")
    session_id = session_data.get("sessionId", "unknown")
    session_file_str = session_data.get("sessionFile")
    if session_file_str:
        session_file = Path(session_file_str)
    else:
        session_file = SESSIONS_DIR / f"{session_id}.jsonl"
    archive_info = None
    timestamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    # Archive predecessor chain first (truncateAfterCompaction=true leaves predecessors untracked)
    pred_count = archive_predecessor_chain(session_file, timestamp)
    if pred_count > 0:
        log(f"   📦 Archived {pred_count} predecessor(s) from compaction chain")
    if session_file.exists():
        archived_name = f"{session_file.stem}.archived.reset.{timestamp}.jsonl"
        archived = SESSIONS_DIR / archived_name
        cleaned_lines, stats = cleanup_jsonl_content(session_file)
        if cleaned_lines:
            if write_cleaned_jsonl(archived, cleaned_lines):
                session_file.unlink()
                log(f"✅ Archived: {session_file.name} → {archived.name}")
                log(f"   🧹 Cleaned: {stats['kept']} kept, {stats['removed']} removed")
                archive_info = {'file': archived_name, 'stats': stats, 'type': 'reset'}
        else:
            session_file.unlink()
            log(f"🗑️  Deleted empty session: {session_file.name}")
            archive_info = {'file': session_file.name, 'stats': stats, 'type': 'reset_deleted'}
    else:
        log(f"⚠️  Session JSONL not found: {session_file.name}")
    # Delete session mapping
    if not SESSIONS_FILE.exists():
        log("⚠️  sessions.json not found")
        return archive_info
    with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
        sessions = json.load(f)
    if session_key in sessions:
        del sessions[session_key]
        with open(SESSIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(sessions, f, indent=2)
        log("✅ Deleted session mapping from sessions.json")
        log("ℹ️  New session will be created on next message")
    else:
        log("⚠️  Session key not found in sessions.json")
    return archive_info


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    """Main entry point"""
    log("")
    log("=" * 60)
    log("🦴 SESSION RESET")
    log(f"⏰ Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log("=" * 60)

    # Get all sessions
    all_sessions = get_all_sessions()
    if not all_sessions:
        log("ℹ️  No analysis agent sessions found")
    else:
        log(f"📊 Found {len(all_sessions)} tracked session(s)")

    # Step 0: Filter sessions that need resetting
    log("")
    log("📏 STEP 0: Checking session reset criteria...")
    sessions, sessions_skipped = filter_sessions_to_reset(all_sessions)
    if sessions_skipped:
        for session_key, info in sessions_skipped.items():
            parts = session_key.split(":")
            session_name = ":".join(parts[2:]) if len(parts) > 2 else session_key
            log(f"   ⏳ Skipped {session_name} ({info['age_days']} days, {info['compactions']} compactions)")
    if sessions:
        log(f"   🎯 {len(sessions)} session(s) need reset")
    else:
        log(f"   ℹ️  No sessions need reset")

    # Step 1: Handle orphaned files
    log("")
    log("🧹 STEP 1: Checking for orphaned session files...")
    orphaned_count, orphan_info = cleanup_orphaned_files()

    # Step 2: Run cleanup of old archives
    log("")
    log("🧹 STEP 2: Running archive cleanup...")
    cleanup_info = cleanup_old_archives()

    # Step 3: Reset tracked sessions
    log("")
    log("🔄 STEP 3: Resetting tracked sessions...")
    if not sessions:
        log(f"ℹ️  No tracked sessions to reset")
    else:
        reset_count = 0
        for session_key, session_data in sessions.items():
            parts = session_key.split(":")
            session_name = ":".join(parts[2:]) if len(parts) > 2 else session_key
            log(f"   🔄 Resetting {session_name}...")
            reset_session(session_key, session_data)
            reset_count += 1
            log(f"   ✅ Reset complete!")
        log(f"")
        log(f"✅ {reset_count} tracked session(s) reset successfully!")

    # Summary
    log("")
    log("=" * 60)
    log("📋 SUMMARY")
    log(f"   • Sessions checked: {len(all_sessions)}")
    log(f"   • Sessions skipped: {len(sessions_skipped)}")
    log(f"   • Sessions reset: {len(sessions)}")
    log(f"   • Orphaned files archived: {orphaned_count}")
    log(f"   • Old archives deleted: {cleanup_info['deleted_count']}")
    log(f"   • Archives retained: {cleanup_info['kept_count']}")
    log("=" * 60)


if __name__ == "__main__":
    main()
