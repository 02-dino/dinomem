#!/usr/bin/env python3
"""
answer.py — the ANSWER LOOP for the dinomem LongMemEval harness.

For each question in the sample, this:
  1. RETRIEVES from the (already-converged) lab memory via dinomem's BASE recall
     path — plain markdown memory (memory/*.md + MEMORY.md). Base has NO vector or
     graph legs, so base recall is lexical over the distilled memory items. (The
     neuron arm overrides retrieval with its richer hybrid path; see --recall.)
  2. COMPOSES an answer with a USER-SELECTABLE model (DINOMEM_BENCH_ANSWER_MODEL),
     routed through the SAME OpenClaw-gateway path dinomem itself uses (portable:
     no hardcoded OpenAI endpoint, works on any provider the user's gateway has).
  3. EMITS the OFFICIAL LongMemEval hypothesis format: JSONL, one line per question
     = {"question_id":..,"hypothesis":..}. That exact shape is what the official
     src/evaluation/evaluate_qa.py scorer (step 6) consumes. Nothing else.

WHY MID-TIER ANSWER MODEL (disclosed in the run stamp): the only variable we want
to measure is memory QUALITY. A frontier reasoner can infer answers from thin
context and mask weak recall; a mid-tier model isolates what the memory actually
surfaced. README recommends a gpt-4o-mini-class model and explains this.

USAGE:
  python3 answer.py --lab <LAB_DIR> --dataset <longmemeval_s.json> \\
      --out <hypotheses.jsonl> [--n 50] [--qids-file qids.txt] \\
      [--model <answer-model>] [--recall base|command] [--topk 8] [--json]

  --recall base    : lexical recall over lab markdown memory (default; the base arm)
  --recall command : shell an external recall command (neuron arm plugs its hybrid
                     recall here via DINOMEM_BENCH_RECALL_CMD) — kept generic so the
                     neuron harness reuses this same answer loop.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import shutil
import time
from pathlib import Path

STOPWORDS = set("""a an the of to in on at for and or but is are was were be been being
this that these those it its as with by from about into over under then than so if
what when where who whom which why how do does did done have has had will would can
could should may might must i you he she we they me him her us them my your his our
their said say says tell told ask asked""".split())


def _fail(msg: str, code: int = 1):
    print(f"[answer] FAIL: {msg}", file=sys.stderr)
    sys.exit(code)


def _log(msg: str):
    print(f"[answer] {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# BASE RECALL — lexical over the lab's distilled markdown memory.
# ---------------------------------------------------------------------------
def _tokenize(text: str) -> list[str]:
    toks = re.findall(r"[a-z0-9]+", text.lower())
    return [t for t in toks if t not in STOPWORDS and len(t) > 1]


def _load_memory_units(lab: Path) -> list[dict]:
    """Load base memory as retrievable units: each memory item .md file, plus the
    MEMORY.md body split into paragraph chunks. Frontmatter kept as context.
    """
    mem = lab / "memory"
    units: list[dict] = []
    if not mem.exists():
        return units
    # per-item files (the distilled memory)
    for p in sorted(mem.glob("*.md")):
        if p.name == "MEMORY.md" or p.name.startswith("_"):
            continue
        try:
            body = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        units.append({"source": p.name, "text": body, "tokens": set(_tokenize(body))})
    # MEMORY.md consolidated body -> paragraph chunks
    md = mem / "MEMORY.md"
    if md.exists():
        try:
            full = md.read_text(encoding="utf-8", errors="replace")
            for i, chunk in enumerate(re.split(r"\n\s*\n", full)):
                chunk = chunk.strip()
                if len(chunk) < 8:
                    continue
                units.append({"source": f"MEMORY.md#{i}", "text": chunk,
                              "tokens": set(_tokenize(chunk))})
        except OSError:
            pass
    return units


def base_recall(question: str, units: list[dict], topk: int) -> list[dict]:
    """Lexical token-overlap scoring (base has no embeddings). Returns topk units
    by overlap, ties broken by shorter unit (denser match).
    """
    qtok = set(_tokenize(question))
    if not qtok:
        return units[:topk]
    scored = []
    for u in units:
        overlap = len(qtok & u["tokens"])
        if overlap == 0:
            continue
        # normalize a little by unit size so a huge blob doesn't always win
        score = overlap + overlap / (1 + len(u["tokens"]) / 50.0)
        scored.append((score, len(u["text"]), u))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [u for _, _, u in scored[:topk]]


def command_recall(question: str, topk: int) -> list[dict]:
    """Neuron-arm hook: shell DINOMEM_BENCH_RECALL_CMD with {q} and {k} placeholders,
    expect JSONL or newline-separated snippets on stdout. Kept generic so the neuron
    harness reuses this answer loop with its hybrid recall.
    """
    cmd_tpl = os.environ.get("DINOMEM_BENCH_RECALL_CMD", "").strip()
    if not cmd_tpl:
        _fail("--recall command requires DINOMEM_BENCH_RECALL_CMD env (with {q}/{k})")
    cmd = cmd_tpl.replace("{q}", question).replace("{k}", str(topk))
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return []
    out = (r.stdout or "").strip()
    if not out:
        return []

    # FORMAT 1 (neuron hybrid_recall): a single JSON BLOCK on stdout with an
    # "answer_candidates" list. Each candidate: {content, uri, final_score, legs,...}.
    # hybrid_recall also emits stray notices (e.g. graph_search 'No node found ...')
    # to stdout even under --json, so slice from the first '{' to the last '}' before
    # parsing rather than json.loads(out) whole.
    block_units = _parse_hybrid_block(out, topk)
    if block_units is not None:
        return block_units

    # FORMAT 2 (generic): JSONL {source,text|content} or bare-text lines.
    units = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            units.append({"source": obj.get("source", "recall"),
                          "text": obj.get("text", obj.get("content", line))})
        except json.JSONDecodeError:
            units.append({"source": "recall", "text": line})
    return units[:topk]

def _parse_hybrid_block(out: str, topk: int) -> list[dict] | None:
    """Parse a neuron hybrid_recall --json block. Returns a list of {source,text}
    units ordered by the tool's own rank, or None if stdout is not a hybrid block
    (so the caller falls through to the generic JSONL parser).

    Robust to leading/trailing non-JSON notices: slice first '{' .. last '}'.
    """
    start = out.find("{")
    end = out.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        obj = json.loads(out[start:end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict) or "answer_candidates" not in obj:
        return None
    cands = obj.get("answer_candidates") or []
    units = []
    for c in cands:
        if not isinstance(c, dict):
            continue
        text = c.get("content") or c.get("text") or ""
        if not text.strip():
            continue
        src = c.get("uri") or c.get("source") or "recall"
        legs = c.get("legs") or ([c["leg"]] if c.get("leg") else [])
        if legs:
            src = f"{src} [{'+'.join(legs)}]"
        units.append({"source": src, "text": text})
    # already rank-ordered by hybrid_recall; keep its order, cap at topk.
    return units[:topk]


# ---------------------------------------------------------------------------
# ANSWER COMPOSITION — via the OpenClaw gateway (same path dinomem uses).
# ---------------------------------------------------------------------------
def _resolve_openclaw() -> str:
    return shutil.which("openclaw") or "/home/linuxbrew/.linuxbrew/bin/openclaw"


ANSWER_SYS = (
    "You are answering a question using ONLY the retrieved memory below. "
    "The memory is what an assistant remembered from earlier conversations. "
    "Answer concisely and directly. If the memory does not contain the "
    "information needed to answer, reply exactly: I don't know. "
    "Do not speculate beyond the memory. For questions about a date or time, "
    "give the specific date/time if present."
)


def _stub_answer(question: str, contexts: list[dict]) -> tuple[str, dict]:
    """OFFLINE-CI answer: NO gateway call, NO spend. Returns the single retrieved
    memory line whose text best token-overlaps the question, verbatim, as the
    'answer'. NOT a real reader model — it does zero synthesis/superseding, so a
    naive-RAG stub arm will surface whatever chunk ranks first (stale OR current).
    That is exactly the naive floor we want offline: it exercises the whole
    pipeline (recall→answer→score) at $0 and lets the stub JUDGE grade contains-
    answer. A real run passes --model for genuine synthesis. Marked stub in meta
    so no offline number is mistaken for a model-answered result."""
    if not contexts:
        return "I don't know.", {"model": "stub", "route": "stub"}
    ql = set(re.findall(r"[a-z0-9]+", question.lower()))
    best, best_ov = contexts[0], -1
    for c in contexts:
        ov = len(ql & set(re.findall(r"[a-z0-9]+", c.get("text", "").lower())))
        if ov > best_ov:
            best, best_ov = c, ov
    return best.get("text", "").strip(), {"model": "stub", "route": "stub",
                                           "overlap": best_ov}


def compose_answer(question: str, contexts: list[dict], model: str,
                   max_tokens: int, timeout: int) -> tuple[str, dict]:
    if model == "stub":
        return _stub_answer(question, contexts)
    ctx_block = "\n\n".join(
        f"[memory {i+1}] ({c.get('source','?')})\n{c['text']}"
        for i, c in enumerate(contexts)
    ) or "(no memory retrieved)"
    prompt = (
        f"{ANSWER_SYS}\n\n=== RETRIEVED MEMORY ===\n{ctx_block}\n\n"
        f"=== QUESTION ===\n{question}\n\n=== ANSWER ==="
    )
    cmd = [_resolve_openclaw(), "capability", "model", "run",
           "--prompt", prompt, "--gateway", "--json"]
    if model:
        cmd += ["--model", model]
    meta = {"model": model or "gateway-default", "route": "gateway"}
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode == 0:
            raw = r.stdout
            start = raw.find("{")
            obj = json.loads(raw[start:] if start != -1 else raw)
            if obj.get("ok") and obj.get("outputs"):
                text = (obj["outputs"][0].get("text") or "").strip()
                meta["provider"] = obj.get("provider")
                meta["model"] = obj.get("model", meta["model"])
                # token usage for the 1b cost metric (shape varies by provider;
                # read defensively from usage{} or top-level, never assume).
                usage = obj.get("usage") or {}
                out0 = obj["outputs"][0] if obj.get("outputs") else {}
                out_usage = out0.get("usage") or {}
                def _pick(*keys):
                    for src in (usage, out_usage, obj):
                        for k in keys:
                            v = src.get(k)
                            if isinstance(v, (int, float)):
                                return int(v)
                    return None
                meta["prompt_tokens"] = _pick("prompt_tokens", "input_tokens", "promptTokens")
                meta["completion_tokens"] = _pick("completion_tokens", "output_tokens", "completionTokens")
                meta["total_tokens"] = _pick("total_tokens", "totalTokens") or (
                    (meta["prompt_tokens"] or 0) + (meta["completion_tokens"] or 0)
                    if (meta.get("prompt_tokens") or meta.get("completion_tokens")) else None)
                if text:
                    return text, meta
        meta["error"] = (r.stderr or "")[:200]
    except subprocess.TimeoutExpired:
        meta["error"] = "timeout"
    except Exception as e:  # noqa: BLE001
        meta["error"] = str(e)[:200]
    return "", meta


# ---------------------------------------------------------------------------
def load_dataset(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        _fail(f"cannot read dataset {path}: {e}")
    if not isinstance(data, list):
        _fail("dataset is not a list of question objects")
    return data


def select_questions(data: list[dict], n: int | None, qids_file: str | None) -> list[dict]:
    if qids_file:
        wanted = [l.strip() for l in Path(qids_file).read_text().splitlines() if l.strip()]
        idx = {q.get("question_id"): q for q in data}
        picked = [idx[q] for q in wanted if q in idx]
        missing = [q for q in wanted if q not in idx]
        if missing:
            _log(f"WARN {len(missing)} qids from file not in dataset: {missing[:5]}...")
        return picked
    if n is not None:
        return data[:n]
    return data


def main():
    ap = argparse.ArgumentParser(description="dinomem LongMemEval answer loop -> official hypothesis JSONL")
    ap.add_argument("--lab", required=True, help="converged lab workspace (from drive_base.py)")
    ap.add_argument("--dataset", required=True, help="LongMemEval-S json (for questions + qids)")
    ap.add_argument("--out", required=True, help="output hypotheses.jsonl (official format)")
    ap.add_argument("--n", type=int, help="use first N questions (sample mode)")
    ap.add_argument("--qids-file", help="file of question_ids (one per line) to answer")
    ap.add_argument("--model", default=os.environ.get("DINOMEM_BENCH_ANSWER_MODEL", ""),
                    help="answer model (default env DINOMEM_BENCH_ANSWER_MODEL, else gateway default)")
    ap.add_argument("--recall", choices=["base", "command"], default="base",
                    help="base=lexical over lab markdown (base arm); command=external recall (neuron arm)")
    ap.add_argument("--topk", type=int, default=8, help="retrieved units per question")
    ap.add_argument("--max-tokens", type=int, default=512)
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--json", action="store_true", help="also print a run summary JSON to stdout")
    args = ap.parse_args()

    lab = Path(args.lab).resolve()
    if not (lab / ".dinomem_lab").exists():
        _fail(f"{lab} is not a harness lab (missing .dinomem_lab sentinel)")
    data = load_dataset(Path(args.dataset))
    questions = select_questions(data, args.n, args.qids_file)
    if not questions:
        _fail("no questions selected")

    units = _load_memory_units(lab) if args.recall == "base" else []
    if args.recall == "base":
        _log(f"loaded {len(units)} base memory unit(s) from {lab/'memory'}")
        if not units:
            _log("WARN: zero memory units — did drive_base.py converge? answers will be 'I don't know'")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n_answered = 0
    n_idk = 0
    n_error = 0
    models_seen: dict[str, int] = {}
    # 1b metric sidecar: per-question retrieval + latency + token telemetry.
    # score.py joins this with the gold sidecars to compute retrieval
    # recall/precision, and aggregates the cost/latency/storage columns.
    retrieval_log_path = Path(str(out_path) + ".retrieval.jsonl")
    rlog = retrieval_log_path.open("w", encoding="utf-8")

    with out_path.open("w", encoding="utf-8") as fh:
        for i, q in enumerate(questions):
            qid = q.get("question_id")
            question = q.get("question", "")
            if not qid:
                _log(f"skip item {i}: no question_id")
                continue
            t_recall0 = time.perf_counter()
            if args.recall == "base":
                ctx = base_recall(question, units, args.topk)
            else:
                ctx = command_recall(question, args.topk)
            recall_ms = round((time.perf_counter() - t_recall0) * 1000, 1)
            # retrieved session ids (gold-attribution key). base_recall units are
            # distilled memory items (source=file); command/rag units may carry a
            # session_id. We record whatever attribution the engine provides.
            retrieved_sids = []
            retrieved_dia_ids = []
            for c in ctx:
                sid = c.get("session_id") or c.get("sid")
                if sid:
                    retrieved_sids.append(str(sid))
                # dia_id: LoCoMo turn-level evidence attribution (absent otherwise)
                did = c.get("dia_id")
                if did:
                    retrieved_dia_ids.append(str(did))
            retrieved_sources = [c.get("source", "?") for c in ctx]
            ctx_chars = sum(len(c.get("text", "")) for c in ctx)
            t_ans0 = time.perf_counter()
            hypothesis, meta = compose_answer(
                question, ctx, args.model, args.max_tokens, args.timeout
            )
            answer_ms = round((time.perf_counter() - t_ans0) * 1000, 1)
            if meta.get("error"):
                n_error += 1
            if not hypothesis:
                hypothesis = "I don't know."
            if hypothesis.strip().lower().rstrip(".") == "i don't know":
                n_idk += 1
            models_seen[meta.get("model", "?")] = models_seen.get(meta.get("model", "?"), 0) + 1
            # OFFICIAL HYPOTHESIS FORMAT — exactly these two fields.
            fh.write(json.dumps({"question_id": qid, "hypothesis": hypothesis}) + "\n")
            # 1b telemetry line (NOT part of the official hypothesis file).
            rlog.write(json.dumps({
                "question_id": qid,
                "retrieved_count": len(ctx),
                "retrieved_session_ids": retrieved_sids,
                "retrieved_dia_ids": retrieved_dia_ids,
                "retrieved_sources": retrieved_sources,
                "context_chars": ctx_chars,
                "recall_ms": recall_ms,
                "answer_ms": answer_ms,
                "prompt_tokens": meta.get("prompt_tokens"),
                "completion_tokens": meta.get("completion_tokens"),
                "total_tokens": meta.get("total_tokens"),
                "answer_model": meta.get("model"),
            }) + "\n")
            n_answered += 1
            if (i + 1) % 10 == 0:
                _log(f"  answered {i+1}/{len(questions)}")

    summary = {
        "out": str(out_path),
        "n_questions": len(questions),
        "n_answered": n_answered,
        "n_idk": n_idk,
        "n_error": n_error,
        "recall": args.recall,
        "topk": args.topk,
        "answer_models_used": models_seen,
        "answer_model_requested": args.model or "gateway-default",
        "retrieval_log": str(retrieval_log_path),
    }
    rlog.close()
    summary_extra = {"retrieval_log": str(retrieval_log_path)}
    _log(f"wrote {n_answered} hypotheses -> {out_path} "
         f"(idk={n_idk}, errors={n_error}, models={models_seen}); "
         f"telemetry -> {retrieval_log_path}")
    if args.json:
        print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
