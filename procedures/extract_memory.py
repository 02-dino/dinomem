#!/usr/bin/env python3
"""
Memory Extraction Script

Extracts memories from archived session files using LLM:
  - Scans all .archived.* files in sessions directory
  - Filters out already-processed archives (deduplication)
  - Calls LLM per archive to extract insights, patterns, lessons, decisions, preferences
  - Writes to memory/YYYY-MM-DD.md (OpenClaw native format)
  - Compacts when file exceeds 6k chars

Run via orchestrator (auto_session_reset.py) or standalone.
Logs to: logs/extract_memory.log
"""

import json
import os
import re
import subprocess
import urllib.request
from pathlib import Path
from datetime import datetime, timedelta

# ─── Configuration ────────────────────────────────────────────────────────────

MIN_MESSAGE_LENGTH = 92
SESSIONS_DIR = Path("DINOMEM_AGENT_SESSIONS_PLACEHOLDER")
MEMORY_DIR = Path(__file__).parent.parent / "memory"
PROCESSED_LOG = MEMORY_DIR / ".processed_archives.json"
COMPACTION_LOG = MEMORY_DIR / ".compaction_counts.json"
LOG_FILE = Path(__file__).parent.parent / "logs" / "extract_memory.log"
MEMORY_MAX_CHARS = 6000

# Ensure dirs exist
LOG_FILE.parent.mkdir(exist_ok=True)
MEMORY_DIR.mkdir(exist_ok=True)


# ═══════════════════════════════════════════════════════════════════════════════
# OPENCLAW CONFIG HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def get_openclaw_default_model():
    """Get the default model from OpenClaw configuration."""
    openclaw_config = Path.home() / ".openclaw" / "openclaw.json"
    if openclaw_config.exists():
        try:
            with open(openclaw_config, 'r') as f:
                config = json.load(f)
            model = config.get('agents', {}).get('defaults', {}).get('model', {}).get('primary')
            if model:
                return model
        except Exception:
            pass
    try:
        result = subprocess.run(
            ["openclaw", "config", "get", "model"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return None


def get_api_key_from_openclaw(model):
    """Auto-extract API key from OpenClaw config based on model."""
    if not model:
        return None
    openclaw_config = Path.home() / ".openclaw" / "openclaw.json"
    if not openclaw_config.exists():
        return None
    try:
        with open(openclaw_config, 'r') as f:
            config = json.load(f)
        env = config.get('env', {})
        providers = config.get('models', {}).get('providers', {})
        if model.startswith("kimi") or "/kimi" in model:
            for provider_name in ['kimi-coding', 'kimi-code']:
                provider = providers.get(provider_name, {})
                if provider.get('apiKey'):
                    return provider['apiKey']
            return env.get('KIMI_API_KEY')
        if model.startswith("gemini") or "/gemini" in model or model.startswith("google"):
            provider = providers.get('google', {})
            if provider.get('apiKey'):
                return provider['apiKey']
            return env.get('GEMINI_API_KEY')
        if model.startswith("openrouter") or "/" in model:
            provider = providers.get('openrouter', {})
            if provider.get('apiKey'):
                return provider['apiKey']
            return env.get('OPENROUTER_API_KEY')
    except Exception:
        pass
    return None


def get_api_base_from_model(model):
    """Auto-detect API base URL from OpenClaw config."""
    if not model:
        return None
    openclaw_config = Path.home() / ".openclaw" / "openclaw.json"
    if not openclaw_config.exists():
        return None
    try:
        with open(openclaw_config, 'r') as f:
            config = json.load(f)
        providers = config.get('models', {}).get('providers', {})
        if model.startswith("kimi") or "/kimi" in model:
            provider = providers.get('kimi-coding', providers.get('kimi-code', {}))
            return provider.get('baseUrl')
        if model.startswith("gemini") or "/gemini" in model or model.startswith("google"):
            provider = providers.get('google', {})
            return provider.get('baseUrl')
        if model.startswith("openrouter") or "/" in model:
            provider = providers.get('openrouter', {})
            return provider.get('baseUrl')
    except Exception:
        pass
    return None


def get_api_format_from_model(model):
    """Get the API format type from OpenClaw config."""
    if not model:
        return "openai"
    openclaw_config = Path.home() / ".openclaw" / "openclaw.json"
    if not openclaw_config.exists():
        return "openai"
    try:
        with open(openclaw_config, 'r') as f:
            config = json.load(f)
        providers = config.get('models', {}).get('providers', {})
        if model.startswith("kimi") or "/kimi" in model:
            provider = providers.get('kimi-coding', providers.get('kimi-code', {}))
            return provider.get('api', 'openai')
        if model.startswith("gemini") or "/gemini" in model or model.startswith("google"):
            provider = providers.get('google', {})
            return provider.get('api', 'google')
        if model.startswith("openrouter") or "/" in model:
            provider = providers.get('openrouter', {})
            return provider.get('api', 'openai')
    except Exception:
        pass
    return "openai"


def get_fallback_config():
    """Get OpenRouter fallback configuration."""
    fallback_model = "google/gemini-2.5-flash"
    fallback_base = "https://openrouter.ai/api/v1"
    fallback_key = None
    openclaw_config = Path.home() / ".openclaw" / "openclaw.json"
    if openclaw_config.exists():
        try:
            with open(openclaw_config, 'r') as f:
                config = json.load(f)
            fallback_key = config.get('models', {}).get('providers', {}).get('openrouter', {}).get('apiKey')
        except Exception:
            pass
    return fallback_model, fallback_key, fallback_base


# ─── LLM Configuration ───────────────────────────────────────────────────────

LLM_MODEL = get_openclaw_default_model()
LLM_API_KEY = os.environ.get("LLM_API_KEY") or get_api_key_from_openclaw(LLM_MODEL)
LLM_API_BASE = os.environ.get("LLM_API_BASE") or get_api_base_from_model(LLM_MODEL)
LLM_MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "3000"))
LLM_ENABLED = bool(LLM_MODEL)


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
    if not isinstance(text, str):
        return ""
    return text.encode('utf-8', 'ignore').decode('utf-8')


def has_meaningful_content(text):
    if not text:
        return False
    return bool(re.search(r'[a-zA-Z0-9\u4e00-\u9fff]', text))


def extract_message_content(message):
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


# ═══════════════════════════════════════════════════════════════════════════════
# DEDUPLICATION
# ═══════════════════════════════════════════════════════════════════════════════

def load_processed_set():
    if PROCESSED_LOG.exists():
        try:
            with open(PROCESSED_LOG, 'r', encoding='utf-8') as f:
                return set(json.load(f))
        except Exception as e:
            log(f"   ⚠️  Error loading processed log: {e}")
            return set()
    return set()


def save_processed_set(processed_set):
    try:
        with open(PROCESSED_LOG, 'w', encoding='utf-8') as f:
            json.dump(sorted(list(processed_set)), f, indent=2)
    except Exception as e:
        log(f"   ⚠️  Error saving processed log: {e}")


def filter_new_archives(archive_files):
    processed = load_processed_set()
    new_archives = [f for f in archive_files if f not in processed]
    skipped_count = len(archive_files) - len(new_archives)
    return new_archives, skipped_count


def mark_archives_processed(archive_names):
    processed = load_processed_set()
    processed.update(archive_names)
    save_processed_set(processed)


# ═══════════════════════════════════════════════════════════════════════════════
# COMPACTION
# ═══════════════════════════════════════════════════════════════════════════════

def load_compaction_counts():
    if COMPACTION_LOG.exists():
        try:
            with open(COMPACTION_LOG, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            log(f"   ⚠️  Error loading compaction counts: {e}")
            return {}
    return {}


def save_compaction_counts(counts):
    try:
        with open(COMPACTION_LOG, 'w', encoding='utf-8') as f:
            json.dump(counts, f, indent=2)
    except Exception as e:
        log(f"   ⚠️  Error saving compaction counts: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# LLM CALLERS
# ═══════════════════════════════════════════════════════════════════════════════

def _make_llm_request(model, api_key, api_base, prompt, max_tokens, reasoning=False, api_format="openai"):
    try:
        if api_format == "anthropic-messages":
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                "X-Api-Key": api_key
            }
            data = {
                "model": model.split('/')[-1] if '/' in model else model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
                "temperature": 0.1
            }
            base = api_base.rstrip('/')
            if not base.endswith('/v1'):
                base = f"{base}/v1"
            endpoint = f"{base}/chat/completions"
        elif api_format == "google":
            headers = {"Content-Type": "application/json"}
            data = {
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.1}
            }
            endpoint = f"{api_base}/models/{model}:generateContent?key={api_key}"
        else:
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}"
            }
            data = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
                "temperature": 0.1,
                "reasoning": {"enabled": reasoning}
            }
            endpoint = f"{api_base}/chat/completions"
        req = urllib.request.Request(
            endpoint,
            data=json.dumps(data).encode('utf-8'),
            headers=headers,
            method='POST'
        )
        with urllib.request.urlopen(req, timeout=120) as response:
            result = json.loads(response.read().decode('utf-8'))
            if api_format == "google":
                content = result['candidates'][0]['content']['parts'][0]['text']
            elif api_format == "anthropic-messages":
                if 'content' in result:
                    content = result['content'][0]['text']
                else:
                    content = result['choices'][0]['message']['content']
            else:
                content = result['choices'][0]['message']['content']
            return True, content
    except Exception as e:
        return False, str(e)


def call_llm(prompt, max_tokens=None, reasoning=False):
    """Call LLM via OpenClaw gateway. Falls back to OpenRouter."""
    max_tokens = max_tokens or LLM_MAX_TOKENS
    full_prompt = prompt
    if max_tokens and max_tokens < 3000:
        full_prompt = f"[Respond in {max_tokens} tokens or less]\n\n{prompt}"
    # Try OpenClaw gateway first
    try:
        log(f"   🔄 Calling OpenClaw gateway ({LLM_MODEL})...")
        import shutil as _shutil
        _oc = _shutil.which("openclaw") or "/home/linuxbrew/.linuxbrew/bin/openclaw"
        result = subprocess.run(
            [_oc, "capability", "model", "run",
             "--prompt", full_prompt, "--gateway", "--json"],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode == 0:
            output = json.loads(result.stdout)
            if output.get("ok") and output.get("outputs"):
                text = output["outputs"][0].get("text", "")
                if text:
                    log(f"   ✅ Gateway call successful ({output.get('provider')}/{output.get('model')})")
                    return True, text
        else:
            log(f"   ⚠️  Gateway call failed: {result.stderr[:200]}")
    except subprocess.TimeoutExpired:
        log(f"   ⚠️  Gateway call timed out")
    except Exception as e:
        log(f"   ⚠️  Gateway call error: {e}")
    # Fallback to OpenRouter
    log(f"   🔄 Falling back to OpenRouter...")
    fallback_model, fallback_key, fallback_base = get_fallback_config()
    if fallback_key and fallback_model:
        success, result = _make_llm_request(
            fallback_model, fallback_key, fallback_base,
            full_prompt, max_tokens, False, "openai"
        )
        if success:
            log(f"   ✅ OpenRouter fallback successful")
            return True, result
        else:
            log(f"   ⚠️  OpenRouter fallback failed: {result}")
    return False, "All LLM calls failed"


# ═══════════════════════════════════════════════════════════════════════════════
# ARCHIVE CONTENT EXTRACTION
# ═══════════════════════════════════════════════════════════════════════════════

def extract_archive_content(archive_files, max_chars=None):
    """Extract content from archived JSONL files for LLM summarization."""
    all_content = []
    total_chars = 0
    cap_enabled = max_chars is not None
    for filename in archive_files:
        filepath = SESSIONS_DIR / filename
        if not filepath.exists():
            continue
        try:
            entries = []
            with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
            compaction_indices = [i for i, e in enumerate(entries) if e.get('type') == 'compaction']
            if compaction_indices:
                last_idx = compaction_indices[-1]
                entries = entries[last_idx:]
            for data in entries:
                entry_type = data.get('type', '')
                if entry_type == 'compaction':
                    summary = data.get('summary', '').strip()
                    if summary:
                        entry = f"[HISTORY - COMPACTED CONTEXT]:\n{summary}\n\n"
                        if cap_enabled and total_chars + len(entry) > max_chars:
                            all_content.append(f"\n... [content truncated at {max_chars} chars] ...")
                            return "".join(all_content)
                        all_content.append(entry)
                        total_chars += len(entry)
                    continue
                message = data.get('message', {})
                role = message.get('role', 'unknown')
                content = message.get('content', [])
                text = ""
                if isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict) and item.get('type') == 'text':
                            text += item.get('text', '')
                elif isinstance(content, str):
                    text = content
                if text.strip():
                    entry = f"[{role.upper()}]: {text.strip()}\n\n"
                    if cap_enabled and total_chars + len(entry) > max_chars:
                        all_content.append(f"\n... [content truncated at {max_chars} chars] ...")
                        return "".join(all_content)
                    all_content.append(entry)
                    total_chars += len(entry)
        except Exception as e:
            log(f"   ⚠️  Error reading {filename}: {e}")
    return "".join(all_content)


# ═══════════════════════════════════════════════════════════════════════════════
# MEMORY PROCESSING
# ═══════════════════════════════════════════════════════════════════════════════

def process_single_archive(archive_filename):
    """Process one archive file through LLM. Returns summary dict or None."""
    if not LLM_ENABLED:
        return None
    content = extract_archive_content([archive_filename])
    if not content.strip():
        log(f"   ℹ️  No content in {archive_filename}, skipping")
        return None
    prompt = f"""You are an AI agent writing your own memory notes.
Read this SINGLE conversation session and extract ONLY knowledge worth recalling later.

Session:
{content}

If this session contains nothing worth remembering (greetings, price checks, transient chatter, or routine operational logs), return EMPTY arrays for all fields.

Required JSON structure:
{{
  "context": "1-2 sentence overview of this specific session, or empty string if nothing memorable",
  "insights": [
    "[factual] Verifiable structural fact or principle that holds true beyond this session",
    "[pattern] Reusable relationship or mechanism that applies beyond this specific case",
    "[lesson] Something learned from a mistake, outcome, or experiment",
    "[uncertain] Unconfirmed signal or hypothesis requiring verification",
    "[preference] Permanent user trait, style, or strategic boundary"
  ],
  "source_scores": [
    "Source name + reliability assessment: accuracy track record, bias direction"
  ],
  "decisions": [
    "[decision] Explicit choice made: what was chosen, what was rejected, and why — must be honored in future sessions"
  ],
  "corrections": [
    "[correction] Something the user corrected the AI about — exact mistake and the correct behavior"
  ],
  "operational": [
    "[operational] Critical fact needed to act correctly: repo names, file paths, tool names, config values, pipeline structure"
  ],
  "user_preferences": [
    "What you learned about the user's style, preferences, or boundaries — permanent traits only"
  ],
  "topics": ["#hashtag topics covered, lowercase, no spaces"]
}}

Rules:
- EVERY insight MUST start with one of: [factual], [pattern], [lesson], [uncertain], [preference]
- [factual] items MUST be structural truths, NOT transient event data or one-time occurrences
- [pattern] items MUST abstract beyond the specific case — state the transferable mechanism
- [lesson] items MUST reflect a concrete takeaway from an outcome, mistake, or experiment
- [source_scores] track reliability over time: which sources or tools have been right/wrong and why
- [decision] items MUST capture what was chosen AND what was rejected — these are commitments to honor in future sessions
- [correction] items MUST capture the exact mistake made AND the correct behavior — highest priority for recall
- [operational] items MUST be specific and actionable: exact names, paths, values — NOT vague descriptions
- EVERY [operational], [decision], [correction] item MUST end with a [ctx:...] tag: one short phrase (max 5 words) describing the session context. Example: [ctx:github push session], [ctx:cron restore fix], [ctx:user correction]. Keep it minimal.
- Return empty arrays if nothing is worth recalling
- No markdown inside JSON values, plain text only
- Focus on RECALLABLE knowledge, not operational logs
- JSON only, no explanation outside the JSON"""
    log(f"   🔄 Analyzing {archive_filename}...")
    success, response = call_llm(prompt, max_tokens=1500, reasoning=False)
    if not success:
        log(f"   ⚠️  LLM failed for {archive_filename}: {response}")
        return None
    try:
        clean_response = response.strip()
        if clean_response.startswith('```json'):
            clean_response = clean_response[7:]
        if clean_response.startswith('```'):
            clean_response = clean_response[3:]
        if clean_response.endswith('```'):
            clean_response = clean_response[:-3]
        clean_response = clean_response.strip()
        llm_output = json.loads(clean_response)
        today = datetime.now().strftime("%Y-%m-%d")
        summary = {
            'type': 'agent_memory',
            'date': today,
            'context': llm_output.get('context', ''),
            'insights': llm_output.get('insights', []),
            'source_scores': llm_output.get('source_scores', []),
            'decisions': llm_output.get('decisions', []),
            'corrections': llm_output.get('corrections', []),
            'operational': llm_output.get('operational', []),
            'user_preferences': llm_output.get('user_preferences', []),
            'topics': llm_output.get('topics', [])
        }
        has_content = (
            summary['context'].strip() or
            summary['insights'] or
            summary['source_scores'] or
            summary['decisions'] or
            summary['corrections'] or
            summary['operational'] or
            summary['user_preferences']
        )
        if not has_content:
            log(f"   ℹ️  Nothing memorable in {archive_filename}, skipping")
            return None
        log(f"   ✅ Extracted {len(summary['insights'])} insights, {len(summary['decisions'])} decisions, {len(summary['corrections'])} corrections, {len(summary['operational'])} operational from {archive_filename}")
        return summary
    except json.JSONDecodeError as e:
        log(f"   ⚠️  JSON parse failed for {archive_filename}: {e}")
        return None
    except Exception as e:
        log(f"   ⚠️  Error processing {archive_filename}: {e}")
        return None


def _strip_meta_tags(text):
    """Strip [ctx:...] and [expires:...] tags for clean comparison/embedding."""
    return re.sub(r'\s*\[(ctx|expires):[^\]]*\]', '', text).strip()

def _get_tei_embedding(text):
    """Get single embedding from TEI. Returns vector or None."""
    try:
        import urllib.request as _ur
        payload = json.dumps({"input": [text], "model": ""}).encode()
        req = _ur.Request("http://localhost:8080/v1/embeddings", data=payload,
                          headers={"Content-Type": "application/json"}, method="POST")
        with _ur.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
        return data["data"][0]["embedding"]
    except Exception:
        return None

def _cosine_sim(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0

def _contradiction_check_items(new_items, memory_dir, threshold=0.85):
    """
    For each new item, find semantically similar items in existing memory.
    If contradiction detected via LLM → supersede old item, keep new.
    If same thing → skip new (dedup).
    If update → supersede old, keep new.
    Returns filtered new_items list.
    """
    if not new_items:
        return new_items

    item_pattern = re.compile(
        r'^(\s*-\s*)\[(operational|decision|correction)\]\s*(.+?)$'
    )
    existing_items = []
    md_files = sorted([f for f in memory_dir.glob("*.md")
                       if f.name != "MEMORY.md" and not f.name.startswith("_")])
    for md_file in md_files:
        lines = md_file.read_text(encoding='utf-8').split('\n')
        for i, line in enumerate(lines):
            if item_pattern.match(line):
                existing_items.append((md_file, i, line, line.strip()))

    if not existing_items:
        return new_items

    kept_new = []
    for new_item in new_items:
        new_clean = _strip_meta_tags(new_item)
        new_vec = _get_tei_embedding(new_clean)
        if new_vec is None:
            kept_new.append(new_item)
            continue

        candidates = []
        for (md_file, line_idx, raw_line, item_text) in existing_items:
            ex_clean = _strip_meta_tags(item_text)
            ex_vec = _get_tei_embedding(ex_clean)
            if ex_vec and _cosine_sim(new_vec, ex_vec) >= threshold:
                candidates.append((md_file, line_idx, raw_line, item_text))

        if not candidates:
            kept_new.append(new_item)
            continue

        candidate_texts = '\n'.join(f'- {_strip_meta_tags(c[3])}' for c in candidates)
        prompt = f"""You are a memory contradiction checker.

New item to store:
{new_clean}

Existing similar items in memory:
{candidate_texts}

Classify the relationship. Reply with JSON only:
{{"verdict": "same" | "update" | "contradiction" | "unrelated", "reason": "one sentence"}}

- same: new item is equivalent to an existing one (skip new)
- update: new item supersedes existing (delete old, keep new)
- contradiction: new item conflicts with existing (delete old, keep new)
- unrelated: different enough to coexist (keep both)"""

        success, response = call_llm(prompt, max_tokens=100, reasoning=False)
        if not success:
            kept_new.append(new_item)
            continue

        try:
            clean = response.strip().lstrip('```json').lstrip('```').rstrip('```').strip()
            result = json.loads(clean)
            verdict = result.get('verdict', 'unrelated')
        except Exception:
            kept_new.append(new_item)
            continue

        if verdict == 'same':
            log(f"   ⏭️  Contradiction check: skipped duplicate item")
            continue
        elif verdict in ('update', 'contradiction'):
            for (md_file, line_idx, raw_line, item_text) in candidates:
                lines = md_file.read_text(encoding='utf-8').split('\n')
                if line_idx < len(lines):
                    lines[line_idx] = ''
                    md_file.write_text('\n'.join(lines), encoding='utf-8')
            log(f"   ♻️  Contradiction check: superseded {len(candidates)} old item(s) ({verdict})")
            kept_new.append(new_item)
        else:
            kept_new.append(new_item)

    return kept_new

def write_memory_file(summary, dedup=True):
    """Write memory summary to memory/YYYY-MM-DD.md. Deduplicates, compacts if needed."""
    today = summary.get('date', datetime.now().strftime("%Y-%m-%d"))
    memory_file = MEMORY_DIR / f"{today}.md"
    file_exists = memory_file.exists()
    context = summary.get('context', '').strip()
    insights = summary.get('insights', [])
    source_scores = summary.get('source_scores', [])
    decisions = summary.get('decisions', [])
    corrections = summary.get('corrections', [])
    operational = summary.get('operational', [])
    prefs = summary.get('user_preferences', [])
    topics = summary.get('topics', [])
    # TTL tagging: append decay hint to operational/decision/correction items
    today_str = summary.get('date', datetime.now().strftime("%Y-%m-%d"))
    from datetime import timedelta
    def ttl_tag(items, days):
        expiry = (datetime.strptime(today_str, "%Y-%m-%d") + timedelta(days=days)).strftime("%Y-%m-%d")
        return [f"{i} [expires:{expiry}]" if "[expires:" not in i else i for i in items]
    operational = ttl_tag(operational, 90)
    decisions = ttl_tag(decisions, 180)
    corrections = ttl_tag(corrections, 365)
    # Contradiction check for high-stakes categories before dedup
    if file_exists:
        decisions = _contradiction_check_items(decisions, MEMORY_DIR)
        corrections = _contradiction_check_items(corrections, MEMORY_DIR)
        operational = _contradiction_check_items(operational, MEMORY_DIR)

    # Deduplicate against existing file
    if dedup and file_exists:
        try:
            existing = memory_file.read_text(encoding='utf-8')
            insights = [i for i in insights if i not in existing]
            source_scores = [s for s in source_scores if s not in existing]
            decisions = [d for d in decisions if d not in existing]
            corrections = [c for c in corrections if c not in existing]
            operational = [o for o in operational if o not in existing]
            prefs = [p for p in prefs if p not in existing]
        except Exception:
            pass
    if not context and not insights and not source_scores and not decisions and not corrections and not operational and not prefs:
        log(f"   ℹ️  Nothing new to add to {memory_file.name}, skipping")
        return True
    # Build new content block
    new_lines = []
    timestamp = datetime.now().strftime("%H:%M")
    new_lines.append(f"<!-- session chunk: {timestamp} -->")
    new_lines.append("")
    if context:
        new_lines.append(context)
        new_lines.append("")
    if insights:
        new_lines.append("## Key Insights")
        for item in insights:
            new_lines.append(f"- {item}")
        new_lines.append("")
    if source_scores:
        new_lines.append("## Source Scores")
        for item in source_scores:
            new_lines.append(f"- {item}")
        new_lines.append("")
    if decisions:
        new_lines.append("## Decisions")
        for item in decisions:
            new_lines.append(f"- {item}")
        new_lines.append("")
    if corrections:
        new_lines.append("## Corrections")
        for item in corrections:
            new_lines.append(f"- {item}")
        new_lines.append("")
    if operational:
        new_lines.append("## Operational")
        for item in operational:
            new_lines.append(f"- {item}")
        new_lines.append("")
    if prefs:
        new_lines.append("## User Preferences")
        for item in prefs:
            new_lines.append(f"- {item}")
        new_lines.append("")
    if topics:
        new_lines.append(" ".join(topics))
        new_lines.append("")
    new_content = "\n".join(new_lines)
    # Step 1: Always append
    try:
        mode = 'a' if file_exists else 'w'
        with open(memory_file, mode, encoding='utf-8') as f:
            if not file_exists:
                f.write(f"# {today}\n\n")
            f.write(new_content)
        action = "appended to" if file_exists else "created"
        log(f"✅ Memory summary {action}: {memory_file.name}")
    except Exception as e:
        log(f"⚠️  Failed to append memory file: {e}")
        return False
    # Step 2: Check total size and compact if needed
    full_content = memory_file.read_text(encoding='utf-8')
    if len(full_content) > MEMORY_MAX_CHARS:
        counts = load_compaction_counts()
        filename = memory_file.name
        count = counts.get(filename, 0)
        if count < 3:
            log(f"   📦 Total ({len(full_content)} chars) exceeds {MEMORY_MAX_CHARS} — compacting (attempt {count + 1}/3)...")
            compacted = _compact_memory_file(full_content, "")
            counts[filename] = count + 1
            save_compaction_counts(counts)
            if compacted and len(compacted) <= MEMORY_MAX_CHARS:
                try:
                    with open(memory_file, 'w', encoding='utf-8') as f:
                        f.write(compacted)
                    log(f"✅ Memory compacted to {len(compacted)} chars: {memory_file.name}")
                    return True
                except Exception as e:
                    log(f"⚠️  Failed to write compacted memory: {e}")
                    return False
            else:
                log(f"   ⚠️  Compaction failed or still over cap, truncating...")
        else:
            log(f"   ⚠️  Compaction limit reached (3/3) for {memory_file.name}, truncating...")
        # Truncate
        truncated = full_content[:MEMORY_MAX_CHARS].rsplit('\n', 1)[0]
        truncated += "\n\n... [truncated to fit cap]\n"
        try:
            with open(memory_file, 'w', encoding='utf-8') as f:
                f.write(truncated)
            log(f"✅ Memory truncated to {len(truncated)} chars: {memory_file.name}")
            return True
        except Exception as e:
            log(f"⚠️  Failed to write truncated memory: {e}")
            return False
    return True


def _compact_memory_file(existing, new_content):
    """Call LLM to merge existing + new memory into a compact single summary."""
    prompt = f"""You are an AI agent compacting your own daily memory notes. Merge the EXISTING notes with the NEW chunk into a single concise summary. Preserve all insights, decisions, and user preferences — deduplicate similar items and drop obsolete ones.

EXISTING NOTES:
{existing}

NEW CHUNK:
{new_content}

Output ONLY valid JSON:
{{
  "context": "1-2 sentence overview",
  "insights": ["[factual]/[prediction]/[uncertain]/[preference] ..."],
  "decisions": ["..."],
  "user_preferences": ["..."],
  "topics": ["#hashtag ..."]
}}

Rules:
- Preserve [factual] / [prediction] / [uncertain] / [preference] prefixes
- Drop duplicates and merge near-identical items
- Drop expired predictions (older than their expires date)
- Keep the most recent phrasing if two items conflict
- If a prediction has an outcome, note it as [factual]
- JSON only, no markdown outside JSON"""
    success, response = call_llm(prompt, max_tokens=2000, reasoning=False)
    if not success:
        return None
    try:
        clean = response.strip()
        if clean.startswith('```json'):
            clean = clean[7:]
        if clean.startswith('```'):
            clean = clean[3:]
        if clean.endswith('```'):
            clean = clean[:-3]
        clean = clean.strip()
        data = json.loads(clean)
        today = datetime.now().strftime("%Y-%m-%d")
        lines = [f"# {today}", ""]
        lines.append(data.get('context', ''))
        lines.append("")
        for section, key in [("## Key Insights", 'insights'), ("## Decisions", 'decisions'), ("## User Preferences", 'user_preferences')]:
            items = data.get(key, [])
            if items:
                lines.append(section)
                for item in items:
                    lines.append(f"- {item}")
                lines.append("")
        topics = data.get('topics', [])
        if topics:
            lines.append(" ".join(topics))
            lines.append("")
        return "\n".join(lines)
    except Exception as e:
        log(f"   ⚠️  Failed to parse compacted memory: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    """Main entry point"""
    log("")
    log("=" * 60)
    log("📝 MEMORY EXTRACTION")
    log(f"⏰ Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log("=" * 60)

    if not LLM_ENABLED:
        log("⚠️  LLM not configured (no model found in OpenClaw config). Skipping memory extraction.")
        return

    # Find all archived files
    all_archives = [f.name for f in SESSIONS_DIR.glob("*.archived.*.jsonl")]
    if not all_archives:
        log("ℹ️  No archived files found")
        return

    log(f"📁 Found {len(all_archives)} archived file(s)")

    # Filter out already processed
    new_archives, skipped_count = filter_new_archives(all_archives)
    if skipped_count > 0:
        log(f"⏭️  Skipped {skipped_count} already-processed archive(s)")

    if not new_archives:
        log("✅ All archives already processed")
        return

    log(f"")
    log(f"🔄 Processing {len(new_archives)} new archive(s)...")

    stored_count = 0
    empty_count = 0
    failed_count = 0

    for archive_name in new_archives:
        summary = process_single_archive(archive_name)
        mark_archives_processed([archive_name])
        if summary:
            success = write_memory_file(summary)
            if success:
                stored_count += 1
            else:
                failed_count += 1
        else:
            empty_count += 1

    log(f"")
    log("=" * 60)
    log("📋 SUMMARY")
    log(f"   • Archives found: {len(all_archives)}")
    log(f"   • Already processed: {skipped_count}")
    log(f"   • New archives processed: {len(new_archives)}")
    log(f"   • Memory entries stored: {stored_count}")
    log(f"   • Empty sessions skipped: {empty_count}")
    log(f"   • Memory write failures: {failed_count}")
    log(f"   • Deduplication tracking: {len(load_processed_set())} archives tracked")
    log("=" * 60)


if __name__ == "__main__":
    main()
