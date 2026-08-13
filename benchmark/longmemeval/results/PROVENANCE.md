# LongMemEval — dataset & license provenance

_Recorded 2026-08-13. This file is the step-1 license gate for the dinomem LongMemEval harness._

## License verdict — PERMISSIVE (clean)

| Artifact | License | Redistribution |
|-|-|-|
| LongMemEval code (`github.com/xiaowu0162/LongMemEval`) | **MIT** — © 2024 Di Wu | ✅ permitted with attribution |
| LongMemEval-S/M/oracle dataset (`hf.co/datasets/xiaowu0162/longmemeval`) | inherits project MIT; no separate restrictive card | ✅ permitted with attribution |
| LongMemEval-V2 (`hf.co/datasets/xiaowu0162/longmemeval-v2`) | **Apache-2.0** | ✅ permitted (not used here; we target v1 LongMemEval-S) |

MIT and Apache-2.0 both allow redistribution. **Vendoring would be legal.**

## Dataset-acquisition decision — FETCH-PINNED, do NOT vendor

Even though the license permits vendoring, we **fetch at runtime pinned to the official HF revision SHA** and do NOT commit the data into this repo, because:

1. **Size.** Full dataset ≈ 3.04 GB; `longmemeval_s` alone ≈ 278 MB. Committing 278 MB bloats every clone of `github/dinomem`. Unacceptable for a public repo.
2. **Reproducibility without bloat.** Pinning to a fixed HF revision SHA gives byte-identical data across runs (the whole point of a citable benchmark) with zero repo weight.
3. **Upstream renamed the files.** On HF the files were renamed (`longmemeval_s.json` → `longmemeval_s`, stored via Xet pointers). A naive fetch of `longmemeval_s.json` 404s. The fetcher MUST resolve the current HF path and pin the revision SHA — never assume the old `.json` filename.

### Fetcher requirements (implemented in the harness)
- Resolve `longmemeval_s` from `hf.co/datasets/xiaowu0162/longmemeval` at a PINNED revision SHA (record the exact SHA in `results/latest.md` for every run).
- Cache locally under a gitignored path (e.g. `benchmark/longmemeval/.data/`); never commit the payload.
- Ship attribution: this PROVENANCE.md + a citation to the LongMemEval paper (Wu et al., ICLR 2025) in the harness README.
- `.gitignore` the `.data/` cache so the payload can never be accidentally committed.

## Attribution (ships in README)
LongMemEval: Benchmarking Chat Assistants on Long-Term Interactive Memory. Di Wu et al., ICLR 2025. Code MIT-licensed © 2024 Di Wu. Dataset: huggingface.co/datasets/xiaowu0162/longmemeval.
