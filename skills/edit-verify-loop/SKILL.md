---
name: edit-verify-loop
description: After editing any CODE file (py/sh/js/ts/json or a shebang script), run scripts/verify.sh on it and loop edit->verify->fix IN THE SAME TURN until it passes — instead of ending the turn on an unverified edit and waiting for a cron or the user to catch the break. Read this before/after any code edit so the fix happens now, in one turn, the way an IDE agent does it.
---

# Edit-verify-loop (dinomem)

Closes the IDE "real-time edit→run→fix" gap **in-turn**, with no cron latency and
no coding-agent subprocess. An IDE agent edits, runs the check, reads the error,
fixes, repeats — 20x in one shot. You do the same with one wrapper the model
loops on.

## The loop (do this every time you edit a code file)

0. **Plan the blast radius before you patch** when the file is non-trivial or shared:
   ```bash
   bash scripts/blast-radius.sh <the-file-you-plan-to-edit>
   ```
   Read the LAST stdout line:
   - `BLAST_RADIUS: SINGLE_FILE :: ...` → start with a one-file patch.
   - `BLAST_RADIUS: COORDINATED :: ...` → plan a coordinated multi-file change before editing.
   - `BLAST_RADIUS: STOP_AND_INSPECT :: ...` → inspect callers/tests/surrounding files first; do not jump straight into a one-file patch.
   - `BLAST_RADIUS: UNKNOWN :: ...` → scope is unclear; inspect manually before patching.

   Rules:
   - Always run this first for shared/load-bearing paths like `scripts/lib/*`, `hooks/*`, installer/update scripts, or anything that smells reused.
   - On a trivial leaf file with an obvious narrow proof, you may skip it.

1. **Edit** the file (`edit` / `apply_patch` / `write`).
2. **Verify immediately** — same turn, no waiting:
   ```bash
   bash scripts/verify.sh <the-file-you-just-edited>
   ```
   (Live analyst path: `bash /root/.openclaw/workspace-analyst/scripts/verify.sh <file>`.)
3. **Read the LAST stdout line.** It is always exactly one of:
   - `VERIFY: PASS <file> (<checker>)` → done, move on.
   - `VERIFY: SKIP <file> (<reason>)` → no checker for this type (or tool
     missing); NOT a failure. Move on; don't chase it.
   - `VERIFY: FAIL <file> (<checker>) :: <first-error>` → **route the repair, then fix and re-loop.**
4. **On FAIL:** pass the failing output to `scripts/repair-hint.sh` before you patch:
   ```bash
   bash scripts/repair-hint.sh verify 'VERIFY: FAIL <file> (<checker>) :: <first-error>'
   ```
   Read the LAST stdout line:
   - `REPAIR_HINT: SYNTAX :: ...` → fix the parser/compiler issue in the edited file first.
   - `REPAIR_HINT: FS :: ...` → fix the file/path issue first.
   - `REPAIR_HINT: CONFIG :: ...` → fix the broken config/data parse first.
   - `REPAIR_HINT: UNKNOWN :: ...` → inspect the raw output manually before another patch.

   Then fix the specific cause and go back to step 2. Repeat **up to 5 iterations in this same turn.**
5. **Never end the turn on an unverified code edit.** If you edited code, the
   turn does not finish until you've seen a `PASS` or `SKIP` for it (or hit the
   5-try cap and reported the stuck error honestly).

## When to run it

- **Always** after editing `.py .sh/.bash .js/.mjs/.cjs .ts/.tsx .json` or a
  shebang script.
- **Skip** for pure prose/markdown/config-data edits (verify.sh SKIPs them
  anyway — running it is just a cheap confirm).

## What verify.sh is (and is NOT)

- It's the fast **syntax/type gate** (`py_compile`, `bash -n`, `node --check`,
  `tsc --noEmit`, `jq empty`) — catches the ~80% of breakage that's a syntax /
  parse / broken-JSON error, at ~zero cost, every edit.
- It is **NOT** the full test suite. Once the gate is green, ask
  `scripts/test-target.sh` for the **smallest meaningful deeper proof** for the
  edited file, then run it if one exists.

## The deeper-proof step (automatic, after green gate)

After `verify.sh` returns `PASS` (or after `diagnose.sh` returns `CLEAN` on a
behavior-sensitive change), run:

```bash
bash scripts/test-target.sh <the-file-you-just-edited>
```

(Live analyst path: `bash /root/.openclaw/workspace-analyst/scripts/test-target.sh <file>`.)

Read the LAST stdout line. It is always exactly one of:
- `TEST_TARGET: <command>` → run that command from the repo root as the deeper proof.
- `TEST_TARGET: NONE` → no narrow repo-local test exists; stop at the green gate unless a broader test is obviously required.

If that test command FAILs, route the next repair first:
```bash
bash scripts/repair-hint.sh test '<failing test output>'
```
- `REPAIR_HINT: TEST :: ...` → inspect the behavior path the direct test exercises, not just syntax.

Selection policy:
- Prefer the **smallest** repo-local proof over a broad suite.
- If the edited file already IS a test file, run that test directly.
- If `test-target.sh` returns `NONE`, do **not** invent a guessy test name.

So the full default loop becomes:
`edit -> verify -> fix if FAIL -> diagnose if needed -> test-target -> run target if present -> dependency-check -> run affected dependent targets if present`

## The dependency-aware step (automatic, after the edited file is green)

After the edited file has passed its own narrowest proof, run:

```bash
bash scripts/dependency-check.sh <the-file-you-just-edited>
```

(Live analyst path: `bash /root/.openclaw/workspace-analyst/scripts/dependency-check.sh <file>`.)

Read the LAST stdout line. It is always exactly one of:
- `DEPENDENCY_CHECK: <command> || <command> ...` → run those command(s) from the repo root, in order. They are already deduped and narrowed through `test-target.sh`.
- `DEPENDENCY_CHECK: NONE` → no graph-backed nearby dependent proof was available; stop at the edited-file proof.

If any dependent command FAILs, route the next repair first:
```bash
bash scripts/repair-hint.sh dependency-test '<failing dependent test output>'
```
- `REPAIR_HINT: DEPENDENT_TEST :: ...` → inspect the caller/importer path that fan-out selected.

Rules:
- This step is **neuron-enhanced, base-safe**. If no `code_query`/graph is available, `dependency-check.sh` returns `NONE` instead of guessing.
- Do **not** invent your own dependent test fan-out when the helper returns `NONE`.
- Run dependency-check only **after** the edited file itself is green; don't widen the blast radius before the direct gate passes.

## The cap (don't spin forever)

Max **5** fix iterations per file in one turn. If still `FAIL` at 5, stop
looping and report the exact `<first-error>` + what you tried — a stuck error
the model can't crack in 5 tries needs a human eye, not a 6th blind attempt.

## Self-check
- [ ] For a non-trivial/shared path, did I run `blast-radius.sh` before the first patch?
- [ ] Did I obey `STOP_AND_INSPECT` / `COORDINATED` instead of forcing a one-file patch anyway?
- [ ] Did I run `verify.sh` on every code file I edited, this turn?
- [ ] Did I loop on FAIL (read the `::` error, fix, re-run), not just once?
- [ ] Did I end with PASS/SKIP for each edited file (or an honest stuck-report at the 5-cap)?
- [ ] For a failure, did I run `repair-hint.sh` before the next patch so I wasn't fixing blind?
- [ ] For a behavior change, did I also run the deeper test once the gate was green?
