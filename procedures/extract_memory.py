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
        if model.startswith("openrouter"):
            provider = providers.get('openrouter', {})
            if provider.get('apiKey'):
                return provider['apiKey']
            return env.get('OPENROUTER_API_KEY')
        # Generic provider/model form (e.g. ninerouter/..., anthropic/...):
        # resolve the real provider from the first path segment instead of
        # assuming OpenRouter. Falls back to OpenRouter only if that provider
        # has no key but OpenRouter does.
        if "/" in model:
            prefix = model.split("/", 1)[0]
            provider = providers.get(prefix, {})
            if provider.get('apiKey'):
                return provider['apiKey']
            env_key = env.get(f"{prefix.upper().replace('-', '_')}_API_KEY")
            if env_key:
                return env_key
            or_provider = providers.get('openrouter', {})
            if or_provider.get('apiKey'):
                return or_provider['apiKey']
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
        if model.startswith("openrouter"):
            provider = providers.get('openrouter', {})
            return provider.get('baseUrl')
        if "/" in model:
            prefix = model.split("/", 1)[0]
            provider = providers.get(prefix, {})
            if provider.get('baseUrl'):
                return provider['baseUrl']
            return providers.get('openrouter', {}).get('baseUrl')
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
        if model.startswith("openrouter"):
            provider = providers.get('openrouter', {})
            return provider.get('api', 'openai')
        if "/" in model:
            prefix = model.split("/", 1)[0]
            provider = providers.get(prefix, {})
            if provider:
                return provider.get('api', 'openai')
            return providers.get('openrouter', {}).get('api', 'openai')
    except Exception:
        pass
    return "openai"


def get_fallback_config():
    """Direct-API fallback when the OpenClaw gateway is unreachable.

    Uses whatever the user already has — no hardcoded provider dependency:
      1. The user's own default model + its provider key/base (works for
         Anthropic, Kimi, Gemini, xAI, OpenRouter, ninerouter, etc.).
      2. OpenRouter only if a key for it happens to be configured.
    Returns (model, key, base) — key is None when nothing is resolvable, in
    which case the caller skips the fallback gracefully.
    """
    # 1) Prefer the user's own default model on its native provider.
    if LLM_MODEL:
        key = get_api_key_from_openclaw(LLM_MODEL)
        base = get_api_base_from_model(LLM_MODEL)
        if key and base:
            return LLM_MODEL, key, base
    # 2) Last resort: OpenRouter, only if the user actually has a key for it.
    openclaw_config = Path.home() / ".openclaw" / "openclaw.json"
    if openclaw_config.exists():
        try:
            with open(openclaw_config, 'r') as f:
                config = json.load(f)
            or_key = config.get('models', {}).get('providers', {}).get('openrouter', {}).get('apiKey')
            if or_key:
                return "google/gemini-2.5-flash", or_key, "https://openrouter.ai/api/v1"
        except Exception:
            pass
    # Nothing resolvable — caller skips fallback (gateway-only).
    return LLM_MODEL, None, None


# ─── LLM Configuration ───────────────────────────────────────────────────────

LLM_MODEL = get_openclaw_default_model()
LLM_API_KEY = os.environ.get("LLM_API_KEY") or get_api_key_from_openclaw(LLM_MODEL)
LLM_API_BASE = os.environ.get("LLM_API_BASE") or get_api_base_from_model(LLM_MODEL)
LLM_MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "3000"))
LLM_ENABLED = bool(LLM_MODEL)
# Optional cost lever (opt-in): no-reasoning (reasoning=False) calls route to this
# model if set. Reasoning calls always use the OpenClaw default. Unset = no change.
CHEAP_MODEL = os.environ.get("DINOMEM_CHEAP_MODEL", "").strip() or None
# Thinking level passed to the gateway for reasoning=True calls.
REASONING_THINKING = os.environ.get("DINOMEM_REASONING_THINKING", "high").strip() or "high"


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
            # OpenAI-compatible proxies (ninerouter, openrouter, etc.) expect the
            # model id WITHOUT the leading routing-provider segment. Strip the
            # first segment when the base looks like a routed proxy, but keep
            # multi-segment ids (e.g. cc/claude-... , xai/grok-...) intact.
            _model_id = model
            _parts = model.split('/')
            if len(_parts) >= 3:
                # provider/group/name -> drop only the provider routing prefix
                _model_id = '/'.join(_parts[1:])
            data = {
                "model": _model_id,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
                "temperature": 0.1,
                "stream": False,
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
            raw = response.read().decode('utf-8')
            # Some OpenAI-compatible proxies stream SSE (`data: {...}` chunks)
            # even for non-stream requests. Reassemble those before parsing.
            if raw.lstrip().startswith('data:'):
                parts = []
                for line in raw.splitlines():
                    line = line.strip()
                    if not line.startswith('data:'):
                        continue
                    payload = line[len('data:'):].strip()
                    if not payload or payload == '[DONE]':
                        continue
                    try:
                        chunk = json.loads(payload)
                        delta = chunk.get('choices', [{}])[0].get('delta', {})
                        piece = delta.get('content') or ''
                        if not piece:
                            msg = chunk.get('choices', [{}])[0].get('message', {})
                            piece = msg.get('content') or ''
                        parts.append(piece)
                    except Exception:
                        continue
                content = ''.join(parts)
                if content:
                    return True, content
                # fall through to error if nothing assembled
                return False, "streamed response had no content"
            result = json.loads(raw)
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
    # Route model by task type (opt-in, default = no change):
    #   reasoning=True  -> OpenClaw default model + thinking level (quality path)
    #   reasoning=False -> DINOMEM_CHEAP_MODEL if set, else OpenClaw default
    _gw_cmd = ["capability", "model", "run",
               "--prompt", full_prompt, "--gateway", "--json"]
    if reasoning:
        _gw_cmd += ["--thinking", REASONING_THINKING]
        _route = f"default+thinking={REASONING_THINKING}"
    elif CHEAP_MODEL:
        _gw_cmd += ["--model", CHEAP_MODEL]
        _route = f"cheap={CHEAP_MODEL}"
    else:
        _route = "default"
    # Try OpenClaw gateway first
    try:
        log(f"   🔄 Calling OpenClaw gateway ({_route})...")
        import shutil as _shutil
        _oc = _shutil.which("openclaw") or "/home/linuxbrew/.linuxbrew/bin/openclaw"
        result = subprocess.run(
            [_oc] + _gw_cmd,
            capture_output=True, text=True, timeout=120
        )
        if result.returncode == 0:
            # Gateway may prepend non-JSON noise (e.g. [state-migrations] warnings)
            # to stdout. Slice from the first '{' so json.loads doesn't choke and
            # trigger a false fallback on an otherwise successful call.
            _raw = result.stdout
            _start = _raw.find("{")
            output = json.loads(_raw[_start:] if _start != -1 else _raw)
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
    # Fallback to direct provider API (user's own default model, no OpenRouter
    # dependency). Only fires if the gateway is unreachable.
    log(f"   🔄 Falling back to direct provider API...")
    fallback_model, fallback_key, fallback_base = get_fallback_config()
    # No-reasoning + cheap model set: prefer cheap model on the fallback too.
    if (not reasoning) and CHEAP_MODEL:
        _ck = get_api_key_from_openclaw(CHEAP_MODEL)
        _cb = get_api_base_from_model(CHEAP_MODEL)
        if _ck and _cb:
            fallback_model = CHEAP_MODEL
            fallback_key = _ck
            fallback_base = _cb
    if fallback_key and fallback_model and fallback_base:
        _fmt = get_api_format_from_model(fallback_model)
        success, result = _make_llm_request(
            fallback_model, fallback_key, fallback_base,
            full_prompt, max_tokens, reasoning, _fmt
        )
        if success:
            log(f"   ✅ Direct fallback successful ({fallback_model})")
            return True, result
        else:
            log(f"   ⚠️  Direct fallback failed: {result}")
    else:
        log(f"   ⚠️  No direct fallback available (gateway-only setup)")
    return False, "All LLM calls failed"


# ═══════════════════════════════════════════════════════════════════════════════
# ARCHIVE CONTENT EXTRACTION
# ═══════════════════════════════════════════════════════════════════════════════

# Max chars per archive before splitting into chunks (split on session boundary)
ARCHIVE_CHUNK_MAX_CHARS = 40000
# Max archives to batch into a single LLM call
BATCH_SIZE = 3
# Max total chars across all archives in a batch
BATCH_MAX_CHARS = 80000

def extract_single_archive_content(filename):
    """Extract content from one archive file. Returns list of session-boundary chunks."""
    filepath = SESSIONS_DIR / filename
    if not filepath.exists():
        return []
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
        # Start from last compaction if present
        compaction_indices = [i for i, e in enumerate(entries) if e.get('type') == 'compaction']
        if compaction_indices:
            entries = entries[compaction_indices[-1]:]
        # Build content, split at session boundaries when chunk exceeds limit
        chunks = []
        current_chunk = []
        current_chars = 0
        for data in entries:
            entry_type = data.get('type', '')
            if entry_type == 'compaction':
                summary = data.get('summary', '').strip()
                if summary:
                    entry = f"[HISTORY - COMPACTED CONTEXT]:\n{summary}\n\n"
                    current_chunk.append(entry)
                    current_chars += len(entry)
                continue
            # Session boundary marker — split chunk here if over limit
            if entry_type == 'session_start' and current_chars >= ARCHIVE_CHUNK_MAX_CHARS:
                if current_chunk:
                    chunks.append("".join(current_chunk))
                current_chunk = []
                current_chars = 0
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
                current_chunk.append(entry)
                current_chars += len(entry)
        if current_chunk:
            chunks.append("".join(current_chunk))
        return chunks if chunks else []
    except Exception as e:
        log(f"   ⚠️  Error reading {filename}: {e}")
        return []

def extract_archive_content(archive_files, max_chars=None):
    """Legacy single-pass extractor. Used for single-archive calls."""
    chunks = []
    for filename in archive_files:
        chunks.extend(extract_single_archive_content(filename))
    content = "".join(chunks)
    if max_chars and len(content) > max_chars:
        content = content[:max_chars] + f"\n... [truncated at {max_chars} chars] ..."
    return content


# ═══════════════════════════════════════════════════════════════════════════════
# MEMORY PROCESSING
# ═══════════════════════════════════════════════════════════════════════════════

def process_batch_archives(archive_filenames):
    """Process multiple archives in a single LLM call. Returns list of summary dicts."""
    if not LLM_ENABLED:
        return []
    # Build combined content with clear archive separators
    sections = []
    total_chars = 0
    included = []
    for filename in archive_filenames:
        chunks = extract_single_archive_content(filename)
        if not chunks:
            continue
        content = "".join(chunks)
        if total_chars + len(content) > BATCH_MAX_CHARS:
            break
        sections.append(f"=== ARCHIVE: {filename} ===\n{content}\n=== END: {filename} ===\n")
        total_chars += len(content)
        included.append(filename)
    if not sections:
        return []
    combined = "\n".join(sections)
    archive_list = ", ".join(included)
    prompt = f"""You are an AI agent writing your own memory notes.
Read these {len(included)} conversation session archives and extract ONLY knowledge worth recalling later.
Each archive is delimited by === ARCHIVE: filename === and === END: filename ===.

Archives: {archive_list}

{combined}

Return a JSON ARRAY with one object per archive (in the same order as the archives above).
If an archive contains nothing worth remembering, include it with all empty arrays.

Each object in the array must have this structure:
{{
  "archive": "filename",
  "context": "1-2 sentence overview, or empty string if nothing memorable",
  "insights": ["[factual|pattern|lesson|uncertain|preference] ..."],
  "source_scores": ["Source name + reliability assessment"],
  "decisions": ["[decision] what was chosen, what rejected, why"],
  "corrections": ["[correction] exact mistake + correct behavior"],
  "operational": ["[operational] exact names/paths/values + behavioral default"],
  "user_preferences": ["permanent user trait or boundary"],
  "topics": ["#hashtag"]
}}

Rules:
- EVERY insight MUST start with [factual], [pattern], [lesson], [uncertain], or [preference]
- [factual] = structural truths, NOT transient events
- [decision] and [correction] = err on side of extracting
- CONFIG/BEHAVIOR CHANGE RULE: if the session changes a config value, default, policy, or behavior affecting future sessions, extract it as [decision] even if not phrased as "we decided" (e.g. "changed default to X", "updated README to reflect ON by default", "switched Y to Z"). State the new value AND the old one it replaces.
- [operational] = specific and actionable, end with [ctx:max 5 words]
- [decision] and [correction] = end with [ctx:max 5 words]
- Return empty arrays if nothing worth remembering for that archive
- JSON array only, no explanation outside JSON"""
    log(f"   🔄 Batch analyzing {len(included)} archives: {archive_list}...")
    success, response = call_llm(prompt, max_tokens=2000 * len(included), reasoning=False)
    if not success:
        log(f"   ⚠️  Batch LLM failed: {response}")
        return []
    try:
        clean = response.strip()
        if clean.startswith('```json'): clean = clean[7:]
        if clean.startswith('```'): clean = clean[3:]
        if clean.endswith('```'): clean = clean[:-3]
        results = json.loads(clean.strip())
        if not isinstance(results, list):
            log("   ⚠️  Batch response not a list, falling back to single processing")
            return []
        today = datetime.now().strftime("%Y-%m-%d")
        summaries = []
        for item in results:
            has_content = (
                item.get('context', '').strip() or
                item.get('insights') or item.get('decisions') or
                item.get('corrections') or item.get('operational') or
                item.get('user_preferences')
            )
            summary = {
                'archive': item.get('archive', ''),
                'type': 'agent_memory',
                'date': today,
                'context': item.get('context', ''),
                'insights': item.get('insights', []),
                'source_scores': item.get('source_scores', []),
                'decisions': item.get('decisions', []),
                'corrections': item.get('corrections', []),
                'operational': item.get('operational', []),
                'user_preferences': item.get('user_preferences', []),
                'topics': item.get('topics', [])
            }
            if has_content:
                log(f"   ✅ {summary['archive']}: {len(summary['insights'])} insights, {len(summary['decisions'])} decisions, {len(summary['corrections'])} corrections, {len(summary['operational'])} operational")
            else:
                log(f"   ℹ️  {summary['archive']}: nothing memorable")
            summaries.append((summary['archive'], summary if has_content else None))
        return summaries
    except json.JSONDecodeError as e:
        log(f"   ⚠️  Batch JSON parse failed: {e}")
        return []
    except Exception as e:
        log(f"   ⚠️  Batch processing error: {e}")
        return []

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
    "[operational] Critical fact needed to act correctly: repo names, file paths, tool names, config values, pipeline structure. MUST include default assumption where relevant — e.g. 'push access confirmed, use github-push.sh, do not ask user' or 'always read X before editing Y'. If the fact implies a behavioral default, state it explicitly."
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
- [decision] items MUST capture what was chosen AND what was rejected — these are commitments to honor in future sessions. Err on the side of extracting — a single decision from a short session is worth storing.
- CONFIG/BEHAVIOR CHANGE RULE: if the session changes a config value, default, policy, or behavior that affects future sessions, extract it as a [decision] even if not explicitly framed as "we decided." Examples: "changed default to X", "updated README to reflect ON by default", "switched from Y to Z." State the NEW value AND the OLD one it supersedes so the contradiction checker can retire the stale fact.
- [correction] items MUST capture the exact mistake made AND the correct behavior — highest priority for recall. Err on the side of extracting — a single correction from a short session is worth storing.
- [operational] items MUST be specific and actionable: exact names, paths, values — NOT vague descriptions
- EVERY [operational], [decision], [correction] item MUST end with a [ctx:...] tag: one short phrase (max 5 words) describing the session context. Example: [ctx:github push session], [ctx:cron restore fix], [ctx:user correction]. Keep it minimal.
- Return empty arrays if nothing is worth remembering
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

    # Match the actual stored format: per-item files store the item as a bare
    # line `[type] text` (no dash). Also accept legacy dash-bullets `- [type] text`.
    # insight/factual included so stale structural facts (e.g. config defaults)
    # can be superseded instead of silently coexisting with a newer decision.
    item_pattern = re.compile(
        r'^\s*-?\s*\[(operational|decision|correction|insight|factual)\]\s*(.+?)$'
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
                # Per-item files store one item as the whole body. If the item
                # line is the only content line, delete the file outright instead
                # of leaving an orphan frontmatter-only file. Legacy multi-item
                # files: blank just the superseded line.
                lines = md_file.read_text(encoding='utf-8').split('\n')
                content_line_idxs = [
                    j for j, ln in enumerate(lines)
                    if ln.strip() and not ln.strip().startswith('---')
                    and not re.match(r'^[a-z_]+:\s', ln.strip())
                ]
                if content_line_idxs == [line_idx]:
                    try:
                        md_file.unlink()
                        log(f"   🗑️  Contradiction check: deleted stale file {md_file.name} ({verdict})")
                    except Exception as _e:
                        log(f"   ⚠️  Could not delete {md_file.name}: {_e}")
                elif line_idx < len(lines):
                    lines[line_idx] = ''
                    md_file.write_text('\n'.join(lines), encoding='utf-8')
                    log(f"   ♻️  Contradiction check: blanked stale line in {md_file.name} ({verdict})")
            kept_new.append(new_item)
        else:
            kept_new.append(new_item)

    return kept_new

def _slugify(text, max_len=40):
    """Convert text to a filesystem-safe slug."""
    import re as _re
    text = text.lower().strip()
    text = _re.sub(r'[^\w\s-]', '', text)
    text = _re.sub(r'[\s_]+', '-', text)
    text = text.strip('-')
    return text[:max_len]

def _write_item_file(memory_dir, date_str, item_type, item_text, topics, context_snippet):
    """Write a single memory item as its own .md file with YAML frontmatter."""
    # Build slug from first 40 chars of item text (strip tags first)
    clean_text = re.sub(r'\s*\[(ctx|expires|\w+):[^\]]*\]', '', item_text).strip()
    clean_text = re.sub(r'^\[(\w+)\]\s*', '', clean_text)  # strip leading [type] tag
    slug = _slugify(clean_text, max_len=40)
    filename = f"{date_str}_{item_type}_{slug}.md"
    filepath = memory_dir / filename
    # Skip if identical file already exists
    if filepath.exists():
        existing = filepath.read_text(encoding='utf-8')
        if item_text in existing:
            return False, filepath  # duplicate
    # Extract expires tag if present
    expires_match = re.search(r'\[expires:([^\]]+)\]', item_text)
    expires = expires_match.group(1) if expires_match else ""
    # Extract ctx tag if present
    ctx_match = re.search(r'\[ctx:([^\]]+)\]', item_text)
    ctx = ctx_match.group(1) if ctx_match else ""
    # Build topic string
    topic_str = ' '.join(topics) if topics else ""
    # YAML frontmatter + content
    frontmatter_lines = [
        "---",
        f"type: {item_type}",
        f"date: {date_str}",
    ]
    if expires:
        frontmatter_lines.append(f"expires: {expires}")
    if ctx:
        frontmatter_lines.append(f"ctx: {ctx}")
    if topic_str:
        frontmatter_lines.append(f"topics: {topic_str}")
    if context_snippet:
        frontmatter_lines.append(f"session_ctx: {context_snippet[:80].replace(chr(10), ' ')}")
    frontmatter_lines.append("---")
    frontmatter_lines.append("")
    frontmatter_lines.append(item_text)
    frontmatter_lines.append("")
    filepath.write_text('\n'.join(frontmatter_lines), encoding='utf-8')
    return True, filepath

def write_memory_file(summary, dedup=True):
    """Write memory summary as per-item files: memory/YYYY-MM-DD_<type>_<slug>.md"""
    today = summary.get('date', datetime.now().strftime("%Y-%m-%d"))
    context = summary.get('context', '').strip()
    insights = summary.get('insights', [])
    source_scores = summary.get('source_scores', [])
    decisions = summary.get('decisions', [])
    corrections = summary.get('corrections', [])
    operational = summary.get('operational', [])
    prefs = summary.get('user_preferences', [])
    topics = summary.get('topics', [])

    # TTL tagging
    from datetime import timedelta
    def ttl_tag(items, days):
        expiry = (datetime.strptime(today, "%Y-%m-%d") + timedelta(days=days)).strftime("%Y-%m-%d")
        return [f"{i} [expires:{expiry}]" if "[expires:" not in i else i for i in items]
    operational = ttl_tag(operational, 90)
    decisions = ttl_tag(decisions, 180)
    corrections = ttl_tag(corrections, 365)

    # Contradiction check for high-stakes categories
    decisions = _contradiction_check_items(decisions, MEMORY_DIR)
    corrections = _contradiction_check_items(corrections, MEMORY_DIR)
    operational = _contradiction_check_items(operational, MEMORY_DIR)
    insights = _contradiction_check_items(insights, MEMORY_DIR)

    # Context snippet for frontmatter (first 80 chars of session context)
    ctx_snippet = context[:80] if context else ""

    # Map item type → list
    item_groups = [
        ('insight', insights),
        ('source_score', source_scores),
        ('decision', decisions),
        ('correction', corrections),
        ('operational', operational),
        ('preference', prefs),
    ]

    written = 0
    skipped = 0
    for item_type, items in item_groups:
        for item in items:
            ok, fpath = _write_item_file(MEMORY_DIR, today, item_type, item, topics, ctx_snippet)
            if ok:
                written += 1
                log(f"   ✅ [{item_type}] → {fpath.name}")
            else:
                skipped += 1

    if written == 0 and skipped == 0:
        log(f"   ℹ️  Nothing to write for {today}")
        return True

    log(f"✅ Wrote {written} item file(s), skipped {skipped} duplicate(s) for {today}")
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
    log(f"🔄 Processing {len(new_archives)} new archive(s) in batches of {BATCH_SIZE}...")

    stored_count = 0
    empty_count = 0
    failed_count = 0

    # Process in batches
    for i in range(0, len(new_archives), BATCH_SIZE):
        batch = new_archives[i:i + BATCH_SIZE]
        if len(batch) == 1:
            # Single archive — use single processor
            summary = process_single_archive(batch[0])
            mark_archives_processed([batch[0]])
            if summary:
                success = write_memory_file(summary)
                stored_count += 1 if success else 0
                failed_count += 0 if success else 1
            else:
                empty_count += 1
        else:
            # Multiple archives — batch LLM call
            results = process_batch_archives(batch)
            if not results:
                # Batch failed — fallback to single processing
                log(f"   ⚠️  Batch failed, falling back to single processing for {batch}")
                for archive_name in batch:
                    summary = process_single_archive(archive_name)
                    mark_archives_processed([archive_name])
                    if summary:
                        success = write_memory_file(summary)
                        stored_count += 1 if success else 0
                        failed_count += 0 if success else 1
                    else:
                        empty_count += 1
            else:
                processed_names = [r[0] for r in results]
                mark_archives_processed(processed_names)
                for archive_name, summary in results:
                    if summary:
                        success = write_memory_file(summary)
                        stored_count += 1 if success else 0
                        failed_count += 0 if success else 1
                    else:
                        empty_count += 1
                # Mark any archives not returned by batch as processed
                for name in batch:
                    if name not in processed_names:
                        mark_archives_processed([name])
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
