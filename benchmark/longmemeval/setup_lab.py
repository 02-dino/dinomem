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

WHAT THIS BUILDS (lab layout mirrors a real dinomem workspace):
  <lab>/
    procedures/   (copied from installed base, SESSIONS_DIR patched)
    tools/        (copied from installed base)
    memory/       (empty; pipeline writes distilled memory here)
    sessions/     (the lab session-archive dir; adapter drops the sample .jsonl here)
    kb/           (vector dbs etc, if the pipeline builds them)
    logs/

USAGE:
  python3 setup_lab.py --source <installed_dinomem_ws> [--lab <dir>] [--json]
  python3 setup_lab.py --teardown <lab_dir>

  --source : an INSTALLED dinomem workspace (has procedures/extract_memory.py).
             Defaults to DINOMEM_WORKSPACE env, else fails loud.
  --lab    : where to build it. Default: a mktemp -d throwaway.

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
    ap.add_argument("--teardown", help="teardown the given lab dir and exit")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = ap.parse_args()

    if args.teardown:
        teardown(args.teardown)
        return

    source = resolve_source(args.source)
    info = build_lab(source, args.lab)
    if args.json:
        print(json.dumps(info, indent=2))
    else:
        print(f"lab workspace: {info['lab']}")
        print(f"  sessions dir (drop sample .jsonl here): {info['sessions_dir']}")
        print(f"  SESSIONS_DIR patched: {info['sessions_dir_patched']}")
        print(f"  source (live, untouched): {info['source']}")
        print(f"teardown with: python3 setup_lab.py --teardown {info['lab']}")


if __name__ == "__main__":
    main()
