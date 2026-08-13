---
name: build-quality
description: Quality floor for code/config the agent BUILDS for itself (a cron gate, hook handler, skill body, tool, lib fn). Read this before writing more than a few lines of new self-modification code, so the build comes out small, DRY, reused, documented, and TESTED — not bloated or copy-pasted.
---

# Build-quality (dinomem)

When you BUILD something for the agent itself — a gate script, hook handler,
skill body, tool, or shared function — hold it to this floor. This is guidance
that raises the average, not a hard gate: an LLM can't be forced to write clean
code the way `route.py verify` forces a landed write. So treat these as the
default you deviate from only with a reason.

## The floor (apply in order)

1. **Reuse before reinvent.** Search for an existing primitive first
   (`grep` the tools/scripts/lib dirs). A helper that already does 90% of it
   beats a fresh 100%. If you're about to write logic that rhymes with
   something already there, call that instead.

2. **DRY — never copy-paste a block twice.** If the same ~5+ lines would appear
   in two places, factor them into ONE shared function (`scripts/lib/*.sh`, a
   module fn) and call it. Two near-identical copies is a bug waiting to
   diverge. (Worked example that shipped WRONG first: two gate scripts each
   carried a 16-line CPU-backpressure block; correct build was one
   `gate_fire_or_defer` in gate_lib.sh + a 1-line call per gate.)
   **Mechanical check:** run `tools/route.py dup <file>` on what you just built
   — exit 1 = it found a repeated block (with line numbers), exit 0 = clean.
   ADVISORY (a heuristic hint, not a verdict): confirm the flag is real
   duplication before refactoring — trivial or conceptually-distinct repeats are
   fine to leave. This catches literal copy-paste the soft rule above misses.

3. **Smallest thing that works.** No dead code, no speculative options "in case
   we need it," no config knobs nobody asked for. Ship the minimum that passes
   the test; add surface only when a real need arrives.

4. **Document the WHY, not the WHAT.** One short header block per unit stating
   *why it exists / what contract it holds / the non-obvious gotcha*. Do NOT
   narrate every line — code says what, comments say why. A hot path with more
   comment lines than code lines is over-documented; trim to the gotcha.

5. **Test, don't assume — debug until it works.** Run the thing at least once.
   Prove the exit status / output on BOTH the normal path AND the edge (empty
   input, busy box, missing file, unknown value). If a routed write, finish
   with `route.py verify <surface> <needle>`; for a script you built, also run
   `route.py dup <file>` (DRY check, step 2). A build you didn't run is not
   done — it's a guess. Fix and re-run until green; never report success on an
   untested path. (Pinning a contract empirically once beats re-deriving its
   polarity from comments — comments can lie, an exit code can't.)

## Self-check before calling it done
- [ ] Did I reuse an existing primitive where one fit?
- [ ] Is any block copy-pasted? (Run `route.py dup <file>`; if flagged and real → factor it.)
- [ ] Any dead code / unused option I can delete?
- [ ] Does each unit have a one-line WHY, and no line-by-line narration?
- [ ] Did I actually RUN it — normal path AND at least one edge — and see it pass?
