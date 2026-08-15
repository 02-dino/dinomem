# dinomem × LongMemEval-S

A **citable, convention-standard** memory benchmark for dinomem. It measures dinomem's memory the way the field measures every memory system: on the public **LongMemEval-S** dataset, scored by LongMemEval's **own official grader**. One dataset, one scorer, one disclosed model pair — nothing custom, nothing to game.

> This is **not** a leaderboard we invented. It's the same benchmark Mem0 / Zep / MIRIX / memory papers publish on, so dinomem's number sits on the same scale as theirs. Compare against the [official LongMemEval results](https://github.com/xiaowu0162/LongMemEval).

## What it measures (and what it doesn't)

LongMemEval is an **outcome** benchmark: it feeds a long timestamped chat history, then asks questions, and grades whether dinomem answered correctly. It measures the *effect* of dinomem's memory (contradiction handling, temporal/`as_of` correctness, multi-session recall, abstention) — **not** the mechanism. It cannot measure the "memory improves over wall-clock time" story (no single-snapshot benchmark can) — that stays a described capability, not a number here.

## How it works (honest by construction)

The harness runs in a **throwaway isolated lab workspace — never your live memory.** It spins up a sandboxed dinomem workspace, feeds the sample through dinomem's **real front door — a session archive** — then **forces dinomem's own pipeline to run, in order, to convergence** before asking anything. This is the crucial correctness step: parts of dinomem are cron-materialized (extraction, cleanup, review — and in neuron: graph, synthesis, promotion). If you queried before those ran, you'd measure a half-built memory and get a wrong, nondeterministic number. So the harness **drives the pipeline synchronously to steady-state**, then freezes and queries. That measures the *actual* system, reproducibly. When the run finishes, the lab workspace is torn down. **Your real memory DBs, session archives, and `MEMORY.md` are never touched.**

```
isolated lab workspace (throwaway; live user WS untouched)
  sample → written as a session-archive .jsonl (real entry point)
         → dinomem pipeline forced IN ORDER to convergence (asserts fail-loud)
         → freeze → answer every question via dinomem recall → OFFICIAL scorer → results/latest.md
teardown lab workspace
```

This repo ships the **base** arm. (The neuron upgrade layer measures itself with a separately-named harness in the neuron repo — richer legs, same protocol.)

> **Why LongMemEval's packed sessions extract at all.** Each sample is a very long
> multi-session history written into a single archive. Extraction **windows** any
> oversized archive into ≤80k-char passes (see [dinomem → Windowing](https://github.com/02-dino/dinomem#windowing-long-sessions-dont-lose-their-tail)),
> so a fact buried deep in the haystack reaches the LLM instead of being lost to a
> timeout-truncated response. Durable first-person facts land in the dedicated
> [`user_fact`](https://github.com/02-dino/dinomem#user_facts-durable-facts-about-you)
> lane. Both are load-bearing for this benchmark, not just nice-to-haves.

## Everything you'd want to tune is user-selectable

Nothing is hardcoded. You pick; we recommend and warn.

| Knob | Env var | Recommendation | Why |
|-|-|-|-|
| **Answer model** | `DINOMEM_BENCH_ANSWER_MODEL` | a **mid-tier** model (gpt-4o-mini class) | The only variable should be *memory quality*. A frontier reasoner can mask weak recall by inferring answers — a mid-tier model isolates what the memory actually surfaced. Disclosed in every run. |
| **Judge model** | `DINOMEM_BENCH_JUDGE_MODEL` | **GPT-4o class** (the convention) | The judge grades answers. Using the same strong judge the papers use is expected and trust-neutral; a weak judge = noisy grades = lost trust. |
| **Mode** | `--sample` (default) / `--full` | `--sample` to smoke-test, `--full` for the citable number | See cost below. |

### Cost — shown before every run

`run.py` **prints a token estimate before it spends anything.** A `--full` run is
**gated**: without `--yes` it prints the estimate (including any cost warning) and
**stops** — so you never launch a paid run blind. `--yes` = "I saw the estimate,
proceed."

**Tokens are the headline — the invariant.** $ figures move with provider pricing and
go stale, so the estimate reports TOKENS by default and $ is **opt-in** (`--show-usd`).
All three buckets, INGEST dominant:

```
est_tokens ≈ N × [ ingest_tokens/Q      # base/neuron only, ~94%, on your CHEAP model
                 + answer_tokens/Q
                 + judge_tokens/Q ]
```

| Mode | Scale | Tokens (base arm) |
|-|-|-|
| `--sample` (default, N=20) | fast smoke check | ~2.8M |
| `--full` (all LongMemEval-S, 500 Q) | the citation-grade number | ~70M |

INGEST is ~94% of that and runs on your **cheap** tier (`DINOMEM_CHEAP_MODEL` →
`compaction.model` anchor). If that tier is **UNSET**, ingest rides your pricey default
model — `run.py` prints a loud **COST WARNING** and the `--full` gate tells you to fix
it first. (That warning is a *token-cost* issue, not a $ figure — it's always shown.)

**Which arm runs?** `--arm` defaults to **what you installed**: neuron if a neuron
overlay is detected (`DINOMEM_BENCH_OVERLAY_CMD` set, or a neuron repo/pipeline stage
found near `--source`), else base. One arm per run; explicit `--arm` always wins.

**Want to price a run-EVERYTHING first?** One command prints the full start-to-finish
budget across **all datasets × all arms** (LongMemEval-S + LoCoMo × rag/base/neuron),
no `--source`, no setup, no spend:

```bash
python3 run.py --estimate-all
```

**The real cost is INGESTION, not answering.** The pipeline reads each question's
whole haystack into memory once per question — that dominates (~94% of tokens). Only
`base` + `neuron` pay it; the `rag` arm ingests locally via TEI (~free). A naive
answer+judge-only estimate under-reports by ~50×.

**Two things the estimate gets right (both shipped after windowing landed):**

1. **Windowing overhead is included.** An oversized haystack is read across ~N
   windowed LLM calls (each re-paying the extraction prompt scaffold), so ingest is
   **not** just `haystack_size × questions`. The estimate applies a `×1.2` windowing
   multiplier — the *realistic* figure, not a floor. (LongMemEval-S ingest is ~138k
   tok/Q, not the raw ~115k.)
2. **Ingest is attributed to your CHEAP model, not the answerer.** Ingest runs on
   dinomem's non-reasoning tier — so a cheap ingest model keeps the run cheap, and an
   UNSET cheap tier (ingest on your pricey default) is what the warning flags.

Price the whole program (all datasets × all arms, no `--source`, no spend):

```bash
python3 run.py --estimate-all              # tokens only (default)
python3 run.py --estimate-all --show-usd   # + indicative $ footnote
```

Full start-to-finish (2,486 Q × 3 arms) ≈ **~274M tokens** (windowed). LongMemEval-S
alone (skip LoCoMo) ≈ **~118M tok**. Per-Q counts are a **FLOOR GUESS** — run a small
`--sample` first to measure the true windowed per-Q tokens before committing.

<details>
<summary>💲 Indicative $ (opt-in, <code>--show-usd</code> — volatile, don't trust as gospel)</summary>

$ moves with provider pricing and repricing, so it's a footnote, not the headline.
With the same ~274M-token whole-program run, blended per tier:

| Tier (you choose) | Whole program |
|-|-|
| budget (flash/mini/haiku) | ~$82 |
| mid (sonnet/gpt-4o) | ~$1,094 |
| frontier (opus/gpt-4-class) | ~$5,471 |

Because ingest (94%) runs on your **cheap** model, the realistic figure with a budget
ingest model + mid answer/judge is **~$180** for the whole thing — vs **~$1,105** if
the cheap tier is UNSET (ingest on the pricey default). Set your cheap model.

</details>

**Recommended setup** (a suggestion, not a lock — override with
`--answer-model`/`--judge-model`):
- Answerer + judge both at a **frontier/mid tier** (gpt-4o, sonnet-4.6, or equivalent).
- **Cross-vendor** — answerer and judge from **different vendors** (Anthropic vs OpenAI
  vs …). Never same-vendor both sides: a same-family judge can favor its own family's
  style (self-scoring bias). `run.py` **warns** if it detects a same-vendor pair.
- So e.g. **sonnet-4.6 answerer + gpt-4o judge** *or* **gpt-4o answerer + sonnet-4.6
  judge** — just not same vendor on both.

## Run

```bash
# smoke test (cheap, default)
python3 run.py --sample

# citation-grade full run (prints cost, asks to confirm)
DINOMEM_BENCH_ANSWER_MODEL=<mid-tier> DINOMEM_BENCH_JUDGE_MODEL=<gpt-4o-class> python3 run.py --full --yes
```

Output lands in `results/latest.md`: mode, N, overall + per-category accuracy, the **disclosed** answer & judge models, dataset revision SHA, date, and a link to the official leaderboard. Two back-to-back sample runs must match (determinism guard) or the tool flags nondeterminism instead of printing a number.

## Data & license

Dataset is **downloaded on demand, pinned to the official HF revision SHA, used, then deleted** — never vendored (278 MB would bloat clones) and never persistently cached. See [`results/PROVENANCE.md`](results/PROVENANCE.md) for the full license verdict (MIT, permissive) and acquisition design.

**Isolation:** the whole run happens in a throwaway lab workspace; nothing writes to your live dinomem workspace, and the downloaded dataset + lab dir are removed on completion.

## Relation to the other `benchmark/` suite

The substring-based suite elsewhere in `benchmark/` is a **free, fast CI regression gate** ("did a change break recall") — internal, not a leaderboard. *This* harness is the **on-demand, citation-grade** external number. Different jobs; both honest.

## Attribution

LongMemEval: Benchmarking Chat Assistants on Long-Term Interactive Memory. Di Wu et al., ICLR 2025. Code MIT © 2024 Di Wu. Dataset: huggingface.co/datasets/xiaowu0162/longmemeval.
