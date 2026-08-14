# Vendored from LongMemEval (official)

These files are copied **verbatim** from the official LongMemEval repository so the
dinomem harness scores with the *authors' own* grader (never a reimplementation).
Vendored only to keep the scoring prompt + aggregation byte-identical and offline-auditable.

- **Source:** https://github.com/xiaowu0162/LongMemEval — `src/evaluation/`
- **Files:** `evaluate_qa.py`, `print_qa_metrics.py`
- **License:** MIT © 2024 Di Wu (LongMemEval authors). See the upstream `LICENSE`.
- **Paper:** LongMemEval: Benchmarking Chat Assistants on Long-Term Interactive
  Memory. Di Wu, Hongwei Wang, Wenhao Yu, Yuwei Zhang, Kai-Wei Chang, Dong Yu.
  ICLR 2025. arXiv:2410.10813

## What is load-bearing here (do NOT edit these files)
- `get_anscheck_prompt(task, question, answer, response, abstention)` — the exact
  per-category judge prompt templates (incl. temporal off-by-one forgiveness and
  the abstention prompt). This prompt IS the benchmark's grading contract; changing
  it would break comparability with every published number.
- Judge decode params: `temperature=0, max_tokens=10, n=1`, label = `"yes" in resp.lower()`.
- `print_qa_metrics.py` hard-asserts `autoeval_label['model'] == 'gpt-4o-2024-08-06'`
  (the canonical judge). Per-category + task-averaged + overall + abstention accuracy.

## How the harness uses them
The harness's `score.py` **imports `get_anscheck_prompt` from this vendored module
unchanged**, and routes the judge chat-completion through the user's OpenClaw
gateway (so it works on any provider) at the same `temperature=0`. Only the
transport changes; the grading prompt + decode determinism stay official.

For a strictly-canonical run (judge = `gpt-4o-2024-08-06`), the harness can instead
invoke this upstream `evaluate_qa.py` directly with `OPENAI_API_KEY` set, then
aggregate with the vendored `print_qa_metrics.py` — identical to how the papers do it.

Only these evaluation scripts are vendored; the rest of LongMemEval (dataset,
retrieval/generation baselines) is fetched on demand or not needed.
