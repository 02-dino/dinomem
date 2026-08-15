#!/usr/bin/env python3
"""
setup_lab.py — build a throwaway ISOLATED dinomem workspace for the LongMemEval
harness, so the benchmark NEVER touches the live user workspace.

WHY THIS EXISTS (safety, non-negotiable):
  The base pipeline writes memory DBs, session archives, MEMORY.md, logs. Running
  it on the live workspace would POLLUTE the user's real memory. So the harness
  runs everything in a sandboxed lab workspace and tears it down after.

THE ISOLATION TRAP (found in code, must be handled):
  Most base pipeline paths are Path(__file__).parent.parent-relative, so when the
  code lives INSIDE the lab dir they auto-sandbox (MEMORY_DIR, LOG_FILE, etc).
  BUT extract_memory.py hardcodes an ABSOLUTE sessions dir:
      SESSIONS_DIR = Path("/root/.openclaw/agents/<agent>/sessions")
  Merely exporting DINOMEM_WORKSPACE does NOT redirect that. So we COPY the base
  procedures/tools into the lab and PATCH the absolute SESSIONS_DIR to the lab's
  own sessions dir. The live install is never modified.

TWO LAYOUTS:
  --layout flat  (DEFAULT, base arm): the WS *is* the lab dir. procedures/tools
    copied from the installed base, extract_memory.py's hardcoded absolute
    SESSIONS_DIR PATCHED to <lab>/sessions. Self-contained, no installer runs.
      <lab>/
        procedures/  tools/  memory/  sessions/  kb/  logs/

  --layout real  (NEURON arm): builds a REAL openclaw-style tree so the neuron
    installer (scripts/install.sh) can be run against it. The installer derives
        SESSIONS_DIR = dirname($WS)/agents/$AGENT_ID/sessions
    (from OPENCLAW_DIR=dirname(WS)), so the WS must be a CHILD of an openclaw root
    that also holds agents/<agent>/sessions — otherwise the derived path escapes
    the sandbox. Layout:
      <lab>/                      <- OPENCLAW_DIR (the sandbox root, carries marker)
        workspace-<agent>/        <- the WS passed as --workspace to installers
          procedures/ tools/ memory/ kb/ logs/   (base copied in; NOT patched —
                                                   neuron installer seds the
                                                   DINOMEM_AGENT_SESSIONS_PLACEHOLDER)
        agents/<agent>/sessions/  <- derived SESSIONS_DIR, INSIDE the sandbox;
                                     adapter drops the sample .jsonl here
    In real layout we do NOT patch SESSIONS_DIR: the neuron overlay installer
    rewrites the placeholder to exactly dirname(WS)/agents/<agent>/sessions, which
    is the sandbox sessions dir by construction. (If the base copy has a hardcoded
    absolute SESSIONS_DIR rather than the placeholder, we STILL patch it to the
    sandbox sessions dir as a floor, so a base-only real-layout run is also safe.)

USAGE:
  python3 setup_lab.py --source <installed_dinomem_ws> [--lab <dir>] [--json]
                       [--layout flat|real] [--agent-id ID]
  python3 setup_lab.py --teardown <lab_dir>

  --source   : an INSTALLED dinomem workspace (has procedures/extract_memory.py).
               Defaults to DINOMEM_WORKSPACE env, else fails loud.
  --lab      : where to build it. Default: a mktemp -d throwaway.
  --layout   : flat (default, base arm) | real (neuron arm, openclaw tree).
  --agent-id : agent id for the real-layout tree. Default: 'lab'.

SAFETY GUARANTEES:
  - The lab dir is a fresh temp dir (or an explicit path you pass); we refuse to
    build into an existing dinomem workspace (guard: must not contain MEMORY.md).
  - Nothing is written outside the lab dir.
  - verify_isolation() records the live source WS mtime so the caller can assert
    it is unchanged after a run.
"""
import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

LAB_MARKER = ".dinomem_lab"  # sentinel proving a dir is OUR throwaway lab


def _fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(2)


def resolve_source(source: str | None) -> Path:
    src = source or os.environ.get("DINOMEM_WORKSPACE")
    if not src:
        _fail("no --source and DINOMEM_WORKSPACE unset; cannot find installed dinomem")
    p = Path(src).resolve()
    if not (p / "procedures" / "extract_memory.py").exists():
        _fail(f"source {p} is not an installed dinomem workspace "
              "(procedures/extract_memory.py missing)")
    return p


def build_lab(source: Path, lab: Path | None) -> dict:
    # 1. Pick/verify the lab dir. Must be fresh — never an existing dinomem WS.
    if lab is None:
        lab = Path(tempfile.mkdtemp(prefix="dinomem_lab_"))
    else:
        lab = Path(lab).resolve()
        if (lab / "MEMORY.md").exists() or (lab / "memory").exists():
            _fail(f"refusing to build lab into {lab}: looks like a real workspace "
                  "(MEMORY.md/memory/ present). Pass a fresh path.")
        lab.mkdir(parents=True, exist_ok=True)

    # sentinel so teardown can prove it's ours before rm -rf
    (lab / LAB_MARKER).write_text(f"dinomem longmemeval lab {uuid.uuid4()}\n")

    # 2. Lab skeleton
    for sub in ("procedures", "tools", "memory", "sessions", "kb", "logs"):
        (lab / sub).mkdir(parents=True, exist_ok=True)

    # 3. Copy base procedures + tools (the code the pipeline runs)
    for sub in ("procedures", "tools"):
        src_sub = source / sub
        if not src_sub.is_dir():
            continue
        for f in src_sub.iterdir():
            if f.is_file() and f.suffix in (".py", ".sh"):
                shutil.copy2(f, lab / sub / f.name)

    # 4. PATCH the absolute SESSIONS_DIR in the lab's extract_memory.py copy so it
    #    reads the LAB sessions dir, not the live agent's. Live copy untouched.
    patched = _patch_sessions_dir(lab / "procedures" / "extract_memory.py",
                                  lab / "sessions")

    live_source_mtime = _tree_mtime(source)

    return {
        "lab": str(lab),
        "source": str(source),
        "sessions_dir": str(lab / "sessions"),
        "memory_dir": str(lab / "memory"),
        "sessions_dir_patched": patched,
        "live_source_mtime": live_source_mtime,
        "marker": LAB_MARKER,
    }


def _patch_sessions_dir(extract_py: Path, lab_sessions: Path) -> bool:
    """Rewrite `SESSIONS_DIR = Path("/abs/.../sessions")` to the lab sessions dir.
    Returns True if a substitution happened. Fail-loud if the anchor is missing
    (means the code changed and this patcher is stale — do NOT silently run
    unsandboxed)."""
    if not extract_py.exists():
        _fail(f"lab extract_memory.py missing at {extract_py}; copy step failed")
    text = extract_py.read_text(encoding="utf-8")
    # match: SESSIONS_DIR = Path("....../sessions")   (single or double quotes)
    pat = re.compile(r'^(SESSIONS_DIR\s*=\s*Path\()\s*["\'][^"\']*["\']\s*(\))',
                     re.MULTILINE)
    new, n = pat.subn(rf'\g<1>"{lab_sessions.as_posix()}"\g<2>', text)
    if n == 0:
        _fail("could not find hardcoded `SESSIONS_DIR = Path(\"...\")` in "
              "extract_memory.py to patch — code shape changed; refusing to run "
              "unsandboxed (would read the live sessions dir).")
    extract_py.write_text(new, encoding="utf-8")
    return True


def _write_lab_config(openclaw_dir: Path, agent_id: str,
                      tei_port: int = 8080,
                      model: str = "intfloat/multilingual-e5-small") -> dict:
    """A1 — write <openclaw_dir>/.openclaw/openclaw.json wiring memorySearch to the
    lab's LOCAL TEI embed server, so OpenClaw's NATIVE memory index builds in-lab
    WITHOUT any OpenAI key.

    WHY (bug #12): the native indexer (memory-core-host-engine-embeddings) resolves
    its embedding client via resolveRemoteEmbeddingBearerClient():
        apiKey  = memorySearch.remote.apiKey  ||  requireApiKey(resolveApiKeyForProvider(provider))
        baseUrl = memorySearch.remote.baseUrl ||  providerConfig.baseUrl || defaultBaseUrl
        request = POST {baseUrl}/v1/embeddings   body={model, input}
    With NO memorySearch block the provider defaults to 'openai' and requireApiKey
    THROWS 'No API key found for provider "openai"' — even though the lab's TEI is
    healthy on :{tei_port}. Setting remote.apiKey (a dummy — TEI ignores the bearer)
    kills the throw; remote.baseUrl points the /v1/embeddings POST at local TEI; a
    NON-'openai' provider name skips OpenClaw's OpenAI-attribution route
    (isNativeOpenAIEmbeddingRoute returns false for any non-openai provider/host).
    This is the SAME config surface the neuron installer's self-check probes
    ('openclaw.json has memorySearch config'), so the lab mirrors a real install
    instead of reimplementing the indexer.

    TEI serves an OpenAI-compatible /v1/embeddings; model default matches the live
    embed model (intfloat/multilingual-e5-small, mean-pooled, 384-dim).
    """
    cfg_dir = openclaw_dir / ".openclaw"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = cfg_dir / "openclaw.json"
    base_url = f"http://localhost:{tei_port}"
    mem_search = {
        # non-'openai' provider name => indexer skips OpenAI attribution + the
        # default OpenAI creds path; it uses remote.{baseUrl,apiKey} verbatim.
        "provider": "tei-local",
        "model": model,
        "remote": {
            "baseUrl": base_url,   # indexer appends /v1/embeddings
            # dummy bearer — TEI does not auth; presence is what stops the
            # requireApiKey('openai') throw.
            "apiKey": "lab-local-tei-no-auth",
        },
    }
    # Config lives under agents.<agent_id> (secret path is agents.*.memorySearch.*)
    # AND at top level, so the indexer resolves it regardless of which scope it
    # reads for this throwaway lab.
    config = {
        "memorySearch": mem_search,
        "agents": {agent_id: {"memorySearch": mem_search}},
    }
    # atomic write (temp + rename), then validate it parses as JSON before use.
    tmp = cfg_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    json.loads(tmp.read_text(encoding="utf-8"))  # fail-loud if malformed
    tmp.replace(cfg_path)
    return {
        "config_path": str(cfg_path),
        "tei_base_url": base_url,
        "embed_model": model,
        "provider": "tei-local",
    }


def _tree_mtime(root: Path) -> float:
    """Newest mtime across the source tree — a cheap 'did anything change' probe
    the caller can re-check after a run to assert the live WS was untouched."""
    latest = 0.0
    for p in root.rglob("*"):
        try:
            latest = max(latest, p.stat().st_mtime)
        except OSError:
            continue
    return latest


def build_lab_real(source: Path, lab: Path | None, agent_id: str) -> dict:
    """NEURON-arm layout: an openclaw-style tree so the neuron installer's DERIVED
    SESSIONS_DIR = dirname(WS)/agents/<agent>/sessions lands INSIDE the sandbox.

    Structure:
      <root>/                     (OPENCLAW_DIR, carries the lab marker)
        workspace-<agent>/        (the WS; base copied in, NOT patched by default)
        agents/<agent>/sessions/  (derived SESSIONS_DIR, inside the sandbox)

    We do NOT patch SESSIONS_DIR here: the neuron overlay installer rewrites the
    DINOMEM_AGENT_SESSIONS_PLACEHOLDER to exactly this path. BUT if the base copy
    has a hardcoded absolute SESSIONS_DIR (not the placeholder), we patch it to the
    sandbox sessions dir as a floor so a base-only real-layout run is still safe.
    """
    if lab is None:
        root = Path(tempfile.mkdtemp(prefix="dinomem_lab_real_"))
    else:
        root = Path(lab).resolve()
        if (root / "MEMORY.md").exists() or (root / "memory").exists():
            _fail(f"refusing to build real lab into {root}: looks like a real "
                  "workspace (MEMORY.md/memory/ present). Pass a fresh path.")
        root.mkdir(parents=True, exist_ok=True)

    (root / LAB_MARKER).write_text(f"dinomem longmemeval REAL lab {uuid.uuid4()}\n")

    ws = root / f"workspace-{agent_id}"
    sessions = root / "agents" / agent_id / "sessions"
    for d in (ws / "procedures", ws / "tools", ws / "memory", ws / "kb",
              ws / "logs", sessions):
        d.mkdir(parents=True, exist_ok=True)

    # Copy base procedures + tools into the WS (the neuron overlay installer will
    # overwrite the subset it enhances; copying base first mirrors real install order).
    for sub in ("procedures", "tools"):
        src_sub = source / sub
        if not src_sub.is_dir():
            continue
        for f in src_sub.iterdir():
            if f.is_file() and f.suffix in (".py", ".sh"):
                shutil.copy2(f, ws / sub / f.name)

    # Floor-patch: only if the copied extract_memory.py has a HARDCODED absolute
    # SESSIONS_DIR (base style). If it's the placeholder (neuron style already), or
    # gets overwritten by the neuron installer, the installer's sed handles it.
    em = ws / "procedures" / "extract_memory.py"
    floor_patched = False
    if em.exists():
        txt = em.read_text(encoding="utf-8")
        if "DINOMEM_AGENT_SESSIONS_PLACEHOLDER" not in txt:
            # base-style hardcoded path present -> floor-patch to sandbox sessions
            floor_patched = _patch_sessions_dir(em, sessions)

    # A1 (bug #12): wire the lab's NATIVE memory index to local TEI so
    # OpenClaw's indexer (the neuron graph leg's dependency) builds in-lab
    # WITHOUT an OpenAI key. Root == OPENCLAW_DIR, so the config goes at
    # <root>/.openclaw/openclaw.json where the indexer resolves it.
    lab_config = _write_lab_config(root, agent_id)

    return {
        "layout": "real",
        "lab": str(root),
        "openclaw_dir": str(root),
        "workspace": str(ws),
        "agent_id": agent_id,
        "source": str(source),
        "sessions_dir": str(sessions),
        "memory_dir": str(ws / "memory"),
        "sessions_dir_patched": floor_patched,
        "lab_config": lab_config,
        "overlay_hint": (f"bash <neuron-repo>/scripts/install.sh --workspace {ws} "
                         f"--agent-id {agent_id} --agree --no-cron --no-auto-base"),
        "live_source_mtime": _tree_mtime(source),
        "marker": LAB_MARKER,
    }

def teardown(lab: str) -> None:
    p = Path(lab).resolve()
    if not (p / LAB_MARKER).exists():
        _fail(f"refusing to teardown {p}: no {LAB_MARKER} sentinel "
              "(not a dinomem lab — will not rm -rf an arbitrary dir)")
    shutil.rmtree(p)
    print(json.dumps({"torn_down": str(p)}))


def main() -> None:
    ap = argparse.ArgumentParser(description="Build/teardown an isolated dinomem lab WS")
    ap.add_argument("--source", help="installed dinomem workspace (default: $DINOMEM_WORKSPACE)")
    ap.add_argument("--lab", help="lab dir to build (default: mktemp -d throwaway)")
    ap.add_argument("--layout", choices=("flat", "real"), default="flat",
                    help="flat (default, base arm) | real (neuron arm, openclaw tree)")
    ap.add_argument("--agent-id", default="lab",
                    help="agent id for --layout real tree (default: lab)")
    ap.add_argument("--teardown", help="teardown the given lab dir and exit")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = ap.parse_args()

    if args.teardown:
        teardown(args.teardown)
        return

    source = resolve_source(args.source)
    if args.layout == "real":
        info = build_lab_real(source, args.lab, args.agent_id)
    else:
        info = build_lab(source, args.lab)
    if args.json:
        print(json.dumps(info, indent=2))
    elif args.layout == "real":
        print(f"REAL-layout lab root: {info['lab']}")
        print(f"  workspace (--workspace for installers): {info['workspace']}")
        print(f"  agent-id: {info['agent_id']}")
        print(f"  sessions dir (drop sample .jsonl here): {info['sessions_dir']}")
        print(f"  floor-patched SESSIONS_DIR: {info['sessions_dir_patched']}")
        print(f"  neuron overlay:\n    {info['overlay_hint']}")
        print(f"teardown with: python3 setup_lab.py --teardown {info['lab']}")
    else:
        print(f"lab workspace: {info['lab']}")
        print(f"  sessions dir (drop sample .jsonl here): {info['sessions_dir']}")
        print(f"  SESSIONS_DIR patched: {info['sessions_dir_patched']}")
        print(f"  source (live, untouched): {info['source']}")
        print(f"teardown with: python3 setup_lab.py --teardown {info['lab']}")


if __name__ == "__main__":
    main()
