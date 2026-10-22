#!/usr/bin/env python3
"""
skill_curator.py — L4 skill-library maintenance for dinomem auto self-improvement.

WHY THIS EXISTS: once an agent can create its own skills, the library grows and
nothing ever removes anything. Narrow near-duplicates pile up, every one of them
costs catalog tokens on every turn, and the useful skills get harder to find.

WHAT IT DOES NOT DO: it never archives anything. It emits an archive PROPOSAL,
with its argument and its numbers, into the SAME pending queue the owner already
reviews (`route.py pending|diff|approve|reject`). Owner ruling 2026-10-22: a
skill may only ever be archived by an explicit human approval. Archiving is also
reversible by construction (move into skills/.archive/, never delete).

EXCLUSIONS (a skill is MANAGED and therefore untouchable when any holds):
  1. it ships with dinomem base      (github/dinomem/skills/)
  2. it ships with dinomem-neuron    (github/dinomem-neuron/skills/)
  3. any cron job references it      (including disabled/paused jobs)
  4. it is pinned                    (skills/<name>/.pinned marker)
Rule 3 mirrors Hermes and is correct: a slow or paused schedule must not be able
to lose its own skill. Rules 1-2 are measured from the repos themselves, so a
future base release needs no edit here.

THE ZERO-USE TRAP (measured 2026-10-22): skill_usage.json is built from SKILL.md
READ calls, so a never-read skill is ABSENT from that file, not present with 0.
Reading the usage file alone makes the zero-use cohort invisible — exactly the
cohort this tool exists to find. Hence usage is computed as:
    listdir(skills/) - keys(usage) - managed
Never-read skills also get a GRACE FLOOR: zero uses on a young skill is absence
of evidence, not proof it is disposable.

Usage:
  skill_curator.py report            # what it sees; writes nothing
  skill_curator.py propose           # file archive proposals into the pending queue
  skill_curator.py report --json
Exit codes: 0 = ok, 1 = nothing to propose (so cron can early-exit).
ZERO LLM.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# ── Tunables ─────────────────────────────────────────────────────────────────
STALE_AFTER_DAYS = 14      # unused this long -> worth mentioning
ARCHIVE_AFTER_DAYS = 30    # unused this long -> worth proposing an archive
GRACE_DAYS = STALE_AFTER_DAYS   # never-used skills are immune until this old
MAX_PROPOSALS_PER_RUN = 3  # same review-fatigue guard as L1's session cap

_WS_DEFAULT = "DINOMEM_WORKSPACE_PLACEHOLDER"
if _WS_DEFAULT.startswith("DINOMEM_"):
    _WS_DEFAULT = str(Path(__file__).resolve().parent.parent)
WORKSPACE = Path(os.environ.get("DINOMEM_WORKSPACE", _WS_DEFAULT))

SKILLS_DIR = WORKSPACE / "skills"
QUEUE_DIR = WORKSPACE / "kb" / "improvement_queue"
PENDING_DIR = WORKSPACE / "kb" / "improvement_pending"
USAGE_FILE = QUEUE_DIR / "skill_usage.json"

# Managed-skill sources. Resolved at runtime so a base/neuron release that adds
# a skill is excluded automatically, with no edit to this file.
MANAGED_REPOS = (
    WORKSPACE / "github" / "dinomem" / "skills",
    WORKSPACE / "github" / "dinomem-neuron" / "skills",
)


# ── Exclusions ───────────────────────────────────────────────────────────
def _repo_skills():
    """Skill names shipped by dinomem base/neuron. Read from the repos at
    runtime so a future release that adds a skill is excluded with no edit."""
    names = set()
    for repo in MANAGED_REPOS:
        try:
            names.update(p.name for p in repo.iterdir() if p.is_dir())
        except OSError:
            continue  # repo absent on this install; not an error
    return names


def _cron_referenced():
    """Skill names mentioned by ANY cron job, including disabled ones.

    A paused or slow schedule must not be able to lose the skill it depends on,
    so a disabled job still protects its skill. Fail-CLOSED: if the cron list
    cannot be read we return a sentinel meaning 'unknown', and the caller then
    proposes nothing rather than risk proposing a skill a job needs.
    """
    blobs, ok = [], False
    for argv in (["openclaw", "cron", "list", "--all"], ["crontab", "-l"]):
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0:
            ok = True
            blobs.append((proc.stdout or "") + (proc.stderr or ""))
    return "\n".join(blobs), ok


def _is_pinned(name):
    return (SKILLS_DIR / name / ".pinned").exists()


def _load_usage():
    try:
        data = json.loads(USAGE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, 0
    counts = data.get("counts", {}) if isinstance(data, dict) else {}
    return counts, int(data.get("updated_at", 0) or 0)


def _age_days(path):
    """Age of the skill body in days, from its oldest surviving timestamp.

    mtime is used rather than ctime because an install/sync rewrites ctime and
    would make every synced skill look newly created — which would hand the
    grace floor to skills that are in fact old and unused.
    """
    try:
        return (time.time() - (SKILLS_DIR / path / "SKILL.md").stat().st_mtime) / 86400.0
    except OSError:
        return 0.0


# ── Survey ──────────────────────────────────────────────────────────────
def survey():
    """Classify every installed skill. -> (rows, meta). Writes nothing.

    Usage is derived as listdir - usage_keys rather than read from the usage
    file alone, because a never-read skill is ABSENT there, not zero. Reading
    the file alone would hide the exact cohort this tool exists to surface.
    """
    meta = {"cron_readable": False, "usage_updated_at": 0, "skills": 0}
    if not SKILLS_DIR.is_dir():
        return [], meta

    counts, updated = _load_usage()
    meta["usage_updated_at"] = updated
    meta["usage_present"] = bool(counts)
    repo = _repo_skills()
    cron_blob, cron_ok = _cron_referenced()
    meta["cron_readable"] = cron_ok

    rows = []
    for entry in sorted(SKILLS_DIR.iterdir()):
        if not entry.is_dir() or not (entry / "SKILL.md").exists():
            continue
        name = entry.name
        uses = int(counts.get(name, 0))
        age = _age_days(name)

        reasons = []
        if name in repo:
            reasons.append("ships with dinomem base/neuron")
        if _is_pinned(name):
            reasons.append("pinned")
        if cron_ok and name in cron_blob:
            reasons.append("referenced by a cron job")

        rows.append({"name": name, "uses": uses, "age_days": round(age, 1),
                     "managed": bool(reasons), "managed_why": reasons})
    meta["skills"] = len(rows)
    return rows, meta


def archive_candidates(rows, meta):
    """Pick which unmanaged skills are worth PROPOSING an archive for.

    Fail-closed on an unreadable cron list: without it we cannot prove a skill
    is unreferenced, and proposing to archive a skill a live job depends on is
    far worse than proposing nothing this run.
    """
    # Fail-CLOSED on a missing/empty usage file. MEASURED 2026-10-22: with no
    # skill_usage.json every skill reads uses=0, so the curator would propose
    # archiving a library that is in fact in daily use. Absent evidence must
    # never be read as evidence of absence — that inversion is the single most
    # destructive failure this tool could have.
    if not meta.get("usage_present"):
        return []
    if not meta.get("cron_readable"):
        return []
    out = []
    for row in rows:
        if row["managed"] or row["uses"] > 0:
            continue
        # Grace floor: zero uses on a young skill is absence of evidence, not
        # proof it is disposable.
        if row["age_days"] < GRACE_DAYS:
            continue
        if row["age_days"] < ARCHIVE_AFTER_DAYS:
            continue
        out.append(row)
    out.sort(key=lambda r: -r["age_days"])
    return out[:MAX_PROPOSALS_PER_RUN]


# ── Proposals ──────────────────────────────────────────────────────────
def _argument_for(row, meta):
    """The case the owner reads before deciding. Numbers, not adjectives.

    Every claim here is falsifiable from the same data the owner can re-run,
    and the window is stated explicitly — 'never read' is only ever true WITHIN
    the 21-day archive retention, and hiding that would overstate the evidence.
    """
    window = "within the archived-session window (21d retention)"
    return (
        f"'{row['name']}' has not been read once {window}, and its SKILL.md is "
        f"{row['age_days']:.0f} days old — past the {ARCHIVE_AFTER_DAYS}-day "
        f"archive threshold and the {GRACE_DAYS}-day grace floor for never-used "
        f"skills. It is not shipped by dinomem base/neuron, not pinned, and no "
        f"cron job (enabled or disabled) references it. Archiving moves it to "
        f"skills/.archive/ and is reversible; it is never deleted. Keep it if "
        f"you expect the task it covers to recur — absence of use over one "
        f"window is weaker evidence than your own intent."
    )


def build_proposals(rows, meta):
    """Render archive candidates as pending-queue proposals (no writes)."""
    now = int(time.time())
    out = []
    for row in archive_candidates(rows, meta):
        out.append({
            "id": f"curator-archive-{row['name']}",
            "status": "pending",
            "created_at": now,
            "surface": "skill",
            "title": f"archive unused skill '{row['name']}'",
            "argument": _argument_for(row, meta),
            "evidence_ref": f"skill_usage.json (updated {meta.get('usage_updated_at', 0)})",
            # MOVE, never remove. MEASURED 2026-10-22: skill_tool.py's `remove`
            # is shutil.rmtree and its backup helper resolves to a path that
            # does not exist on this layout, so it silently deletes. The gate's
            # `archive` verb moves the directory into skills/.archive/ instead,
            # which makes "reversible" a property of the operation rather than
            # a claim resting on a backup that may never have run.
            "action": {"archive": row["name"]},
        })
    return out


def emit(proposals):
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    written = 0
    for prop in proposals:
        target = PENDING_DIR / f"{prop['id']}.json"
        if target.exists():
            continue  # already awaiting the owner; never nag twice
        target.write_text(json.dumps(prop, ensure_ascii=False, indent=2),
                          encoding="utf-8")
        written += 1
    return written


def main():
    ap = argparse.ArgumentParser(
        description="L4 skill curator (zero-LLM). Proposes archives; never archives.")
    ap.add_argument("op", choices=["report", "propose"])
    ap.add_argument("--json", dest="as_json", action="store_true")
    args = ap.parse_args()

    rows, meta = survey()
    if not rows:
        msg = f"no skills found under {SKILLS_DIR}"
        print(json.dumps({"ok": True, "skills": 0, "reason": msg})
              if args.as_json else msg)
        return 1

    cands = archive_candidates(rows, meta)
    if args.op == "report":
        if args.as_json:
            print(json.dumps({"ok": True, "meta": meta, "skills": rows,
                              "archive_candidates": [c["name"] for c in cands]},
                             ensure_ascii=False, indent=2))
        else:
            managed = [r for r in rows if r["managed"]]
            print(f"skills: {len(rows)}  managed/excluded: {len(managed)}  "
                  f"curatable: {len(rows) - len(managed)}")
            if not meta.get("usage_present"):
                print(f"  WARNING: no usage data ({USAGE_FILE.name}) -> proposing "
                      "nothing. Every skill reads uses=0 without it, which would "
                      "propose archiving skills that are actually in use. Run "
                      "improvement_scan.py first.")
            if not meta["cron_readable"]:
                print("  WARNING: cron list unreadable -> proposing nothing "
                      "(cannot prove a skill is unreferenced)")
            for r in rows:
                tag = "managed" if r["managed"] else ""
                why = f"  ({'; '.join(r['managed_why'])})" if r["managed_why"] else ""
                print(f"  {r['name']:34s} uses={r['uses']:<4d} age={r['age_days']:<6.1f} {tag}{why}")
            print(f"\narchive candidates: {[c['name'] for c in cands] or 'none'}")
        return 0 if cands else 1

    proposals = build_proposals(rows, meta)
    written = emit(proposals)
    if args.as_json:
        print(json.dumps({"ok": True, "proposed": written,
                          "items": [p["id"] for p in proposals]}, indent=2))
    else:
        print(f"archive proposals filed: {written} ({PENDING_DIR})")
        for p in proposals:
            print(f"  [{p['id']}] {p['title']}")
        if written:
            print("\nreview: route.py pending | route.py diff <id> | route.py approve <id>")
    return 0 if written else 1


if __name__ == "__main__":
    sys.exit(main())
