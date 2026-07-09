## FAQ

**Does it work without Docker?**
TEI requires Docker. Without it, `memory_search` falls back to OpenClaw's built-in search (less accurate). Use `--no-docker` to skip TEI setup and configure a remote embedding server manually.

**How much disk space does it use?**
TEI model: ~80MB. Memory files: minimal (text only). Vector DB grows with usage — roughly 1–2MB per 1000 memory entries.

**Does it work on Windows?**
Not natively. Use WSL2 with Ubuntu.

**Will it affect my existing agent config?**
The installer patches `openclaw.json` and appends to `AGENTS.md`. It does not delete anything. Use `--force` only to overwrite existing scripts. See [OpenClaw config patches](../README.md#openclaw-config-patches) for the exact keys it writes.

**Should I set `reserveTokens` and `keepRecentTokens`?**
See [Compaction tuning](../README.md#compaction-tuning-manual-strongly-recommended) in the README.

**What LLM does it use for memory extraction?**
Your OpenClaw default model via the gateway. Falls back to OpenRouter (`google/gemini-2.5-flash`) if the gateway call fails. To cut cost, set `DINOMEM_CHEAP_MODEL` (routes the no-reasoning bulk scripts). Note: this covers dinomem's own scripts only — OpenClaw's compaction and memory-flush turns are separate, same-tier levers (`compaction.model` and `compaction.memoryFlush.model`). See [Model selection](../README.md#model-selection) and [Compaction tuning](../README.md#compaction-tuning-manual-strongly-recommended) in the README.

**What happens at 100k memories? Does review scale?**
Memory stays bounded by design, not just by deletion. dinomem is not append-only — items expire via TTL, get deleted by daily batched review, and get merged by daily dedup. In practice, 5,000 sessions rarely produces 5,000 memories because redundant and stale items are continuously removed.

For large collections, `memory_review.py` uses batched review (adaptive N files per run, full cycle ~7 days) and an embedding pre-filter (TEI clusters similar files, conflict candidates reviewed first). Review never loads all memories at once — it scales with collection size, not against it.

**How is this different from OpenClaw's built-in memory?**
See [Why dinomem is different](../README.md#why-dinomem-is-different) in the README.

Short version: OpenClaw retrieves memories. dinomem creates and maintains them.

**How are prompts designed for extraction?**
`extract_memory.py` uses structured prompts with explicit output format and per-item tagging: `[factual]`, `[pattern]`, `[decision]`, `[uncertain]`, `[preference]`, `[lesson]`, etc. Each item is extracted independently with a confidence signal. Not freeform — the LLM is constrained to produce structured, typed output.

**How many memories are extracted per session?**
One file per item, not one file per session. A session with 10 distinct facts produces 10 files. Daily dedup in `memory_cleanup.py` merges near-duplicates via semantic similarity, so the total stays lean over time.

**How does it avoid hallucinated facts?**
Two layers: (1) the extraction prompt instructs the LLM to tag uncertain items as `[uncertain]` rather than assert them as facts, and (2) `memory_review.py` runs daily in batches and flags or deletes items that can't be validated against subsequent context.

**How does it handle uncertainty?**
`[uncertain]` items are stored separately and treated differently from `[factual]`. They are not auto-deleted — they stay until the daily batched review processes their file. When reviewed, the LLM promotes them to `[valid]` if subsequent context confirms, keeps them as `[uncertain]` if still unresolved, or removes them if classified as noise. Uncertainty doesn't block storage — it gates promotion.

**How are conflicting memories resolved?**
`contradiction_check.py` runs before every write and checks new items against existing memory. Conflicts are flagged. Daily batched `memory_review.py` resolves them — keeping the more recent or better-evidenced item.

**TEI won't download my embedding model — "relative URL without a base" error?**
This is a known bug in the TEI `cpu-1.5` image when fetching certain models (e.g. `intfloat/multilingual-e5-small`) — it fails on `config.json` with a "relative URL without a base" error. This is a TEI image issue, not a model or network problem. Workaround: use the `cpu-1.6` image tag instead (set in `docker/docker-compose.tei.yml` or your `docker run` command). `cpu-1.6` serves the same models cleanly.

**How do I set `max-input-length` for a custom embedding model?**
As of TEI `1.6.1`, `--max-input-length` is not a valid CLI flag — passing it will crash-loop the container. TEI auto-derives `max_input_length` from the model's own config instead (e.g. 512 for `intfloat/multilingual-e5-small`, vs 256 for the default `all-MiniLM-L6-v2`). If you swap in a custom model, do not pass `--max-input-length` manually — check the model's `/info` endpoint after startup to confirm the derived value.

**`openclaw config patch` (RPC) rejects my config change with a "protected path" error — how do I still set it?**
Some `openclaw.json` paths (e.g. `models.providers.<id>.models`, `agents.defaults.memorySearch.model`) are protected from the gateway's `config.patch`/`config.apply` RPC to prevent accidental corruption via raw edits. This is intentional — do not try to bypass it with a raw file edit. Use the OpenClaw CLI writer instead, which is allowed to touch these paths directly:
```bash
openclaw config set <dot.path> <value> --strict-json --replace
```
Example (matches what dinomem's own migration used): `openclaw config set agents.defaults.memorySearch.model '"intfloat/multilingual-e5-small"' --strict-json --replace`. Run `openclaw config validate` afterward to confirm the file is still valid, then `openclaw gateway restart` to apply.

