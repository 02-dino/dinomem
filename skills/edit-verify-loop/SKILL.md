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
   - `VERIFY: FAIL <file> (<checker>) :: <first-error>` → **fix and re-loop.**
4. **On FAIL:** read the `<first-error>` after `::`, fix the specific cause, go
   back to step 2. Repeat **up to 5 iterations in this same turn.**
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
- It is **NOT** the full test suite. When a change needs deeper proof (behavior,
  integration), run the project's own test after the gate is green — e.g.
  `bash test/<thing>_test.sh` or `pytest <path>`. The gate first (cheap), the
  suite second (only when green and warranted).

## The cap (don't spin forever)

Max **5** fix iterations per file in one turn. If still `FAIL` at 5, stop
looping and report the exact `<first-error>` + what you tried — a stuck error
the model can't crack in 5 tries needs a human eye, not a 6th blind attempt.

## Self-check
- [ ] Did I run `verify.sh` on every code file I edited, this turn?
- [ ] Did I loop on FAIL (read the `::` error, fix, re-run), not just once?
- [ ] Did I end with PASS/SKIP for each edited file (or an honest stuck-report at the 5-cap)?
- [ ] For a behavior change, did I also run the deeper test once the gate was green?
