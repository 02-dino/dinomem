#!/usr/bin/env python3
"""
supersession_build.py — Phase 3 (3a): the SUPERSESSION / RETENTION test-set builder.

WHAT PHASE 3a PROVES (and Phases 1-2 cannot):
  Phase 1 = static retrieval. Phase 2 = accuracy-vs-sessions curve (does the corpus
  track the LATEST truth as sessions pile up). Phase 3a asks the harder BITEMPORAL
  question the strategy PDF demands: when a fact is REPLACED, does the system

    (A) return the CURRENT truth for a "now" query          (supersession-correct), AND
    (B) still recover the OLD truth for an as-of-PAST query  (history-recovery) —
        i.e. it RETIRES old facts (valid_until stamped, moved to _history), it does
        NOT delete them.

  A naive RAG floor CANNOT do either: it has no supersession, so old + new values
  both sit in the index at near-identical similarity (Phase-2 already showed the
  0.862/0.862/0.860 three-way tie). It cannot say which is current, and it has no
  notion of "as of a past date". Neuron's valid_time layer (valid_from / valid_until
  / subject / retire-to-_history / is_valid_at) is EXACTLY the mechanism under test.

METRICS (score-side, computed by supersession_run.py):
  supersession_correct_pct  = of superseded subjects, fraction whose NOW-query
                              returns the CURRENT value (and not a stale one).
  history_recovery_pct      = of superseded subjects, fraction whose AS-OF-PAST
                              query returns the correct HISTORICAL value.
  These two together = "replaces old truth WHILE preserving history" (done_when).

HOW (reuses Phase-1 lab format UNCHANGED so drive/answer/score run as-is):
  A scripted set of SUBJECTS, each with a chain of dated values
  (e.g. dino.location: Jakarta@2024-01 -> Bali@2025-03 -> Lisbon@2026-02).
  We emit the whole chain as ONE dated session archive (each value stated on its
  own dated turn). Then two GOLD probe sets per subject:
    * NOW probe  (question_date = latest+1d): gold = the LATEST value.
    * PAST probe (question_date = a date between an earlier value's from and the
                  next value's from): gold = that EARLIER value.
  Every gold sidecar carries `as_of` (the query instant) + `subject` + the value's
  valid interval, so the neuron arm can feed as_of into hybrid_recall --as-of and
  the score can check bitemporal correctness. Deterministic (seeded).

Subcommands:
  build   --out <dir> [--seed N]                          -> subjects.json
  emit    --subjects <s.json> --lab <labroot>             -> dated archive in lab
  gold    --subjects <s.json> --lab <labroot> --out <f>   -> NOW+PAST probe set + sidecars
  schema  --subjects <s.json>                             -> human summary
"""
from __future__ import annotations

import argparse
import json
import random
import time
import uuid
from pathlib import Path

SCHEMA_VERSION = "phase3-3a-2026-08-14"

def _fail(msg: str) -> None:
    raise SystemExit(f"[supersession_build] FAIL: {msg}")

def _hexid() -> str:
    return uuid.uuid4().hex[:24]

def _iso_from(date_str: str, hour: int = 12) -> str:
    """'YYYY-MM-DD' -> full ISO at the given UTC hour (turn timestamp)."""
    t = time.strptime(date_str, "%Y-%m-%d")
    epoch = int(time.mktime(t)) + hour * 3600
    # time.mktime is local; normalize to a stable UTC-ish string by re-formatting
    # from the parsed fields directly (we only need day-resolution correctness).
    return f"{date_str}T{hour:02d}:00:00"

def _plus_days(date_str: str, days: int) -> str:
    t = time.strptime(date_str, "%Y-%m-%d")
    epoch = time.mktime(t) + days * 86400
    return time.strftime("%Y-%m-%d", time.localtime(epoch))

# ── the supersession script ──────────────────────────────────────────────────
# Each SUBJECT is a stable key answering one question; `chain` = dated (valid_from,
# value, turn-template) updates in chronological order. The CURRENT value = last in
# the chain. A PAST value = any earlier link; its valid interval is
# [its valid_from, next link's valid_from).
SUBJECTS = [
    {"id": "dino.location", "subject": "dino.location",
     "q": "Which city do I currently live in?",
     "q_past_tpl": "Which city was I living in as of {when}?",
     "chain": [
        {"from": "2024-01-15", "value": "Jakarta",
         "tpl": "I'm settled in Jakarta now."},
        {"from": "2025-03-10", "value": "Bali",
         "tpl": "Big move — I've relocated to Bali."},
        {"from": "2026-02-01", "value": "Lisbon",
         "tpl": "New chapter: I just moved to Lisbon."},
     ]},
    {"id": "dino.job", "subject": "dino.job",
     "q": "What is my current job title?",
     "q_past_tpl": "What was my job title as of {when}?",
     "chain": [
        {"from": "2024-02-01", "value": "junior analyst",
         "tpl": "Started a new role as a junior analyst."},
        {"from": "2025-06-01", "value": "senior analyst",
         "tpl": "Got promoted — I'm a senior analyst now."},
        {"from": "2026-04-01", "value": "head of research",
         "tpl": "Career update: I'm now head of research."},
     ]},
    {"id": "dino.car", "subject": "dino.car",
     "q": "What car do I currently drive?",
     "q_past_tpl": "What car was I driving as of {when}?",
     "chain": [
        {"from": "2024-05-01", "value": "a used Honda Civic",
         "tpl": "Bought a used Honda Civic to get around."},
        {"from": "2026-01-15", "value": "a Tesla Model 3",
         "tpl": "Traded up — I drive a Tesla Model 3 now."},
     ]},
    {"id": "dino.phone", "subject": "dino.phone",
     "q": "What phone do I currently use?",
     "q_past_tpl": "What phone was I using as of {when}?",
     "chain": [
        {"from": "2024-03-01", "value": "an iPhone 12",
         "tpl": "Still on my iPhone 12."},
        {"from": "2025-09-01", "value": "an iPhone 15",
         "tpl": "Upgraded to an iPhone 15."},
        {"from": "2026-03-01", "value": "a Pixel 9",
         "tpl": "Switched sides — I'm on a Pixel 9 now."},
     ]},
    {"id": "dino.diet", "subject": "dino.diet",
     "q": "What diet do I currently follow?",
     "q_past_tpl": "What diet was I following as of {when}?",
     "chain": [
        {"from": "2024-06-01", "value": "pescatarian",
         "tpl": "Eating pescatarian these days."},
        {"from": "2025-11-01", "value": "vegetarian",
         "tpl": "Dropped fish — fully vegetarian now."},
     ]},
    # a STABLE control: single value, never superseded. Both NOW and (any) PAST
    # query must return the same value. Confirms the harness doesn't over-retire.
    {"id": "dino.birthplace", "subject": "dino.birthplace",
     "q": "Where was I born?",
     "q_past_tpl": "Where was I born (as of {when})?",
     "chain": [
        {"from": "2024-01-10", "value": "Surabaya",
         "tpl": "For the record, I was born in Surabaya."},
     ]},
]

FILLER = [
    "The weather was pleasant today.",
    "Caught up on some reading in the evening.",
    "Had a productive afternoon.",
    "Tried a new recipe for dinner.",
    "Went for a walk by the river.",
]

def build_subjects(seed: int) -> dict:
    """Return the full spec: subjects + derived NOW/PAST probe schedule.
    Deterministic given seed (only filler placement uses the rng)."""
    rng = random.Random(seed)
    out_subjects = []
    for s in SUBJECTS:
        chain = s["chain"]
        # derive each link's valid interval [from, next.from)
        links = []
        for i, link in enumerate(chain):
            vfrom = link["from"]
            vuntil = chain[i + 1]["from"] if i + 1 < len(chain) else None
            links.append({"valid_from": vfrom, "valid_until": vuntil,
                          "value": link["value"], "tpl": link["tpl"],
                          "is_current": i == len(chain) - 1})
        out_subjects.append({
            "id": s["id"], "subject": s["subject"], "q": s["q"],
            "q_past_tpl": s["q_past_tpl"],
            "superseded": len(chain) > 1,
            "links": links,
        })
    return {"schema": SCHEMA_VERSION, "seed": seed,
            "n_subjects": len(out_subjects),
            "n_superseded": sum(1 for s in out_subjects if s["superseded"]),
            "subjects": out_subjects,
            "_filler_seed": seed}

# ── emit: subjects -> ONE dated session archive (Phase-1 shape) ───────────────
def emit_archive(spec: dict, labroot: Path) -> dict:
    """Write every subject's whole value-chain as dated turns into one archive.
    Each turn is timestamped at its value's valid_from and tagged with
    subject + value + valid_from so a retrieved chunk is attributable and the
    driver's extractor can carry the date. Turns are emitted in chronological
    order across all subjects (interleaved by date), mirroring a real timeline."""
    sess_dir = labroot / "sessions"
    sess_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%dT%H-%M-%S", time.gmtime())
    out = sess_dir / f"supersession.archived.{stamp}.jsonl"

    # flatten all links to dated turns, sort by valid_from
    turns = []
    for s in spec["subjects"]:
        for link in s["links"]:
            turns.append({"date": link["valid_from"], "subject": s["subject"],
                          "value": link["value"], "text": link["tpl"]})
    turns.sort(key=lambda t: t["date"])

    # sprinkle deterministic filler between real turns
    rng = random.Random(spec.get("_filler_seed", 0))
    first_date = turns[0]["date"] if turns else "2024-01-01"
    header = {"type": "session", "version": 3, "id": _hexid(),
              "timestamp": _iso_from(first_date), "cwd": str(labroot)}
    lines = [json.dumps(header)]
    prev = None
    n_msg = 0
    for turn in turns:
        # occasional filler turn (dated same day) before a real one
        if rng.random() < 0.5:
            mid = _hexid()
            lines.append(json.dumps({
                "type": "message", "id": mid, "parentId": prev,
                "timestamp": _iso_from(turn["date"], hour=9),
                "subject": None,
                "message": {"role": "user",
                            "content": [{"type": "text", "text": rng.choice(FILLER)}]},
            }))
            prev = mid
            n_msg += 1
        mid = _hexid()
        lines.append(json.dumps({
            "type": "message", "id": mid, "parentId": prev,
            "timestamp": _iso_from(turn["date"]),
            "subject": turn["subject"], "value": turn["value"],
            "valid_from": turn["date"],
            "message": {"role": "user",
                        "content": [{"type": "text", "text": turn["text"]}]},
        }))
        prev = mid
        n_msg += 1
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"path": str(out), "messages": n_msg, "subjects": len(spec["subjects"])}

# ── gold: NOW + PAST probe set (answer.py-shaped + as_of sidecars) ────────────
def _now_query_date(spec: dict) -> str:
    """A 'now' anchor = latest valid_from across all subjects, +30 days."""
    latest = max(link["valid_from"]
                 for s in spec["subjects"] for link in s["links"])
    return _plus_days(latest, 30)

def build_probes(spec: dict) -> list:
    """Return the flat probe list (NOW + PAST per subject). Each probe carries the
    query date `as_of`, the gold value, the subject, and the probed value interval."""
    now_date = _now_query_date(spec)
    probes = []
    for s in spec["subjects"]:
        links = s["links"]
        current = links[-1]
        # NOW probe: gold = current value, as_of = now anchor
        probes.append({
            "kind": "now",
            "subject": s["subject"],
            "question": s["q"],
            "answer": current["value"],
            "as_of": now_date,
            "valid_from": current["valid_from"],
            "valid_until": current["valid_until"],
            "superseded": s["superseded"],
        })
        # PAST probes: only for superseded subjects — one per EARLIER link, asked
        # as-of a date safely inside that link's interval (its from +15 days, which
        # is < the next link's from because chains are spaced by months).
        if s["superseded"]:
            for link in links[:-1]:
                as_of = _plus_days(link["valid_from"], 15)
                probes.append({
                    "kind": "past",
                    "subject": s["subject"],
                    "question": s["q_past_tpl"].format(when=as_of),
                    "answer": link["value"],
                    "as_of": as_of,
                    "valid_from": link["valid_from"],
                    "valid_until": link["valid_until"],
                    "superseded": True,
                })
    return probes

def write_gold(spec: dict, labroot: Path, out_questions: Path) -> dict:
    """Write per-probe gold sidecars into <labroot>/sessions AND an answer.py-shaped
    question array. Probe ids: <subject>__<kind>[__<as_of>] so NOW/PAST never collide."""
    probes = build_probes(spec)
    sess_dir = labroot / "sessions"
    sess_dir.mkdir(parents=True, exist_ok=True)
    questions = []
    for p in probes:
        subj_slug = p["subject"].replace(".", "_")
        qid = (f"{subj_slug}__now" if p["kind"] == "now"
               else f"{subj_slug}__past__{p['as_of']}")
        qtype = f"supersession-{p['kind']}"
        gold = {
            "question_id": qid,
            "question": p["question"],
            "answer": p["answer"],
            "question_type": qtype,
            "subject": p["subject"],
            "as_of": p["as_of"],
            "valid_from": p["valid_from"],
            "valid_until": p["valid_until"],
            "superseded": p["superseded"],
            "is_adversarial": False,
        }
        (sess_dir / f"{qid}.gold.json").write_text(
            json.dumps(gold, indent=2), encoding="utf-8")
        questions.append({"question_id": qid, "question": p["question"],
                          "answer": p["answer"], "question_type": qtype,
                          "as_of": p["as_of"], "subject": p["subject"]})
    out_questions.write_text(json.dumps(questions, indent=2), encoding="utf-8")
    n_now = sum(1 for p in probes if p["kind"] == "now")
    n_past = sum(1 for p in probes if p["kind"] == "past")
    return {"n_probes": len(probes), "n_now": n_now, "n_past": n_past,
            "questions": str(out_questions)}

def main() -> None:
    ap = argparse.ArgumentParser(description="Phase 3a supersession/retention builder")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pb = sub.add_parser("build", help="build subjects.json")
    pb.add_argument("--out", required=True)
    pb.add_argument("--seed", type=int, default=3141)

    pe = sub.add_parser("emit", help="emit dated archive into lab")
    pe.add_argument("--subjects", required=True)
    pe.add_argument("--lab", required=True, help="labroot (parent of sessions/)")
    pe.add_argument("--json", action="store_true")

    pg = sub.add_parser("gold", help="write NOW+PAST probe gold + question file")
    pg.add_argument("--subjects", required=True)
    pg.add_argument("--lab", required=True)
    pg.add_argument("--out", required=True)

    ps = sub.add_parser("schema", help="human summary")
    ps.add_argument("--subjects", required=True)

    args = ap.parse_args()

    if args.cmd == "build":
        spec = build_subjects(args.seed)
        outdir = Path(args.out)
        outdir.mkdir(parents=True, exist_ok=True)
        (outdir / "subjects.json").write_text(json.dumps(spec, indent=2), encoding="utf-8")
        print(json.dumps({"subjects": str(outdir / "subjects.json"),
                          "n_subjects": spec["n_subjects"],
                          "n_superseded": spec["n_superseded"]}, indent=2))
        return

    if args.cmd == "emit":
        spec = json.loads(Path(args.subjects).read_text())
        info = emit_archive(spec, Path(args.lab))
        print(json.dumps(info, indent=2) if args.json
              else f"emitted {info['path']} ({info['messages']} msgs, {info['subjects']} subjects)")
        return

    if args.cmd == "gold":
        spec = json.loads(Path(args.subjects).read_text())
        info = write_gold(spec, Path(args.lab), Path(args.out))
        print(json.dumps(info, indent=2))
        return

    if args.cmd == "schema":
        spec = json.loads(Path(args.subjects).read_text())
        print(f"schema={spec['schema']} seed={spec['seed']} "
              f"subjects={spec['n_subjects']} superseded={spec['n_superseded']}")
        for s in spec["subjects"]:
            chain = " -> ".join(f"{l['value']}@{l['valid_from']}" for l in s["links"])
            print(f"  {s['subject']:<18} {chain}")
        return

if __name__ == "__main__":
    main()
