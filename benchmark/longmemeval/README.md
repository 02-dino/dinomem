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

## Everything you'd want to tune is user-selectable

Nothing is hardcoded. You pick; we recommend and warn.

| Knob | Env var | Recommendation | Why |
|-|-|-|-|
| **Answer model** | `DINOMEM_BENCH_ANSWER_MODEL` | a **mid-tier** model (gpt-4o-mini class) | The only variable should be *memory quality*. A frontier reasoner can mask weak recall by inferring answers — a mid-tier model isolates what the memory actually surfaced. Disclosed in every run. |
| **Judge model** | `DINOMEM_BENCH_JUDGE_MODEL` | **GPT-4o class** (the convention) | The judge grades answers. Using the same strong judge the papers use is expected and trust-neutral; a weak judge = noisy grades = lost trust. |
| **Mode** | `--sample` (default) / `--full` | `--sample` to smoke-test, `--full` for the citable number | See cost below. |

### Cost — shown before every run

`run.py` **prints a cost estimate before it spends anything**, and `--full` requires an explicit confirm (`--yes`). Estimate:

```
est_cost ≈ N_questions × (answer_tokens + judge_tokens)/question × price(selected models)
```

| Mode | Scale | Ballpark* |
|-|-|-|
| `--sample` (default, ~50 Q) | fast smoke check | ~$1–3 |
| `--full` (all LongMemEval-S) | the citation-grade number | ~$10–25 |

\* Depends entirely on your selected answer+judge models — the printed estimate uses *your* choices, not these defaults. These are for a mid-tier answer + GPT-4o judge.

**Want to price a run-EVERYTHING first?** One command prints the full start-to-finish
budget across **all datasets × all arms** (LongMemEval-S + LoCoMo × rag/base/neuron),
no `--source`, no setup, no spend:

```bash
python3 run.py --estimate-all
```

**The real cost is INGESTION, not answering.** The pipeline reads each question's
whole haystack (~115k tok for LongMemEval-S, ~25k for LoCoMo) into memory once per
question — that dominates (~93% of tokens). Only `base` + `neuron` pay it; the `rag`
arm ingests locally via TEI (~free). A naive answer+judge-only estimate under-reports
by ~50×.

Full start-to-finish (2,486 Q × 3 arms) ≈ **~230M tokens**. Tokens are the invariant;
**$ depends on the models YOU pick** — the command prints a price-tier table so you map
tokens→$ for your provider:

| Tier (you choose) | Whole run |
|-|-|
| budget (flash/mini/haiku) | ~$70 |
| mid (sonnet/gpt-4o) | ~$920 |
| frontier (opus/gpt-4-class) | ~$4,600 |

**Recommended setup** (a suggestion, not a lock — free to override with
`--answer-model`/`--judge-model`):
- Answerer + judge both at a **frontier/mid tier** (gpt-4o, sonnet-4.6, or equivalent).
- **Cross-vendor** — answerer and judge from **different vendors** (Anthropic vs OpenAI
  vs …). Never same-vendor both sides: a same-family judge can favor its own family's
  style (self-scoring bias). `run.py` **warns** if it detects a same-vendor pair.
- So e.g. **sonnet-4.6 answerer + gpt-4o judge** (≈ ~$930) *or* **gpt-4o answerer +
  sonnet-4.6 judge** — just not same vendor on both.

Per-Q token counts are a **FLOOR GUESS** — run a small `--sample` first to measure real
per-Q tokens before committing the full budget. LongMemEval-S alone (skip LoCoMo) is
~118M tok (~half).

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
