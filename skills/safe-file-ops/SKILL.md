---
name: safe-file-ops
description: How to edit files without corruption + run exec safely + write large files without payload-truncation stalls. Read BEFORE any file edit, large write (>6KB), or multi-step exec. Covers read-first/exact-oldText/verify-after, atomic-batch rollback trap, incremental large-write, and exec single-command discipline. Complements edit-verify-loop (this = pre-edit hygiene; that = post-edit syntax gate).
version: 1
---
# safe-file-ops
Read this before editing any file, writing a large file (>~6KB), or running multi-step exec.

This is the PRE-edit hygiene half. Its sibling `edit-verify-loop` is the POST-edit
syntax-gate half — read that one after editing a code file. They do not overlap.

## exec discipline (E1 — single command only)
Prevents sandbox/exec-layer rejections.
- ONE direct command per exec call. Absolute paths.
- FORBID: `&&` `;` `||`, pipes `|`, heredoc (`<<`), inline `cat > f <<EOF`, `cd X && cmd`.
- File doesn't exist? Create via `write` tool, not heredoc.
- Multiple steps? Multiple exec calls, or a wrapper script.

## file editing (no corruption)
tools: read | write | edit. apply_patch = exec subtool (multi-hunk/multi-file); allowed when write is.
pre-edit:
  - Read the file fresh first (never trust remembered content).
  - Copy-paste EXACT oldText from the read output — never guess.
  - JSON >100 lines? Use `jq`, not edit.
  - Critical file? Snapshot first: `python3 DINOMEM_WORKSPACE_PLACEHOLDER/procedures/workspace_backup.py` (or your platform's backup path). Never hand-roll `cp .bak`.
process: read fresh -> backup if critical -> edit (edit=single region; apply_patch=multi-hunk) -> VERIFY the changed region (re-read/grep) -> done.
atomic_batch_rule:
  why: "A multi-edit batch is ATOMIC — if ANY one oldText fails to match, the WHOLE batch rolls back and NOTHING changes. Narrating 'done' before reading the result makes the claim FALSE."
  trap: "A later oldText anchor that overlaps/depends on text an earlier edit in the SAME batch changed -> stale anchor -> full rollback. Same for re-using an anchor a prior applied edit consumed."
  rules:
    - Never claim success until you've read the tool result. Error = zero applied.
    - After any >1-edit batch to a note/config/critical file, grep/re-read the changed lines to CONFIRM.
    - Keep batches DISJOINT/independent. Overlapping or dependent edits -> split into sequential calls, verify between.
    - On rollback: re-read FRESH (anchors may reflect a partial earlier success), rebuild oldText, retry once.
  if_still_fails: STOP and report "Edit error, manual intervention needed". Never report done on an error result.

## large writes (avoid payload-truncation stalls)
Root cause (proven): a single write/edit whose CONTENT is large (~24KB/576-line was the fail case) hits an output ceiling -> the tool-call payload truncates mid-generation -> turn dies with NO tool result. Looks like a narration stall; real cause is payload size.
rule: any file build with expected content >~6KB -> NEVER one inline write.
  - step1: `write` the header/imports only (small payload).
  - step2: grow via successive `edit` append-blocks, each = ONE logical section, <=6KB.
  - step3: syntax/import-check after final block (ast.parse / node parse) — hand off to edit-verify-loop.
smell_check: estimate bytes before a big write; unsure or >6KB -> go incremental. Applies to write/edit/apply_patch AND large exec heredocs. Inline single-shot "to save turns" costs MORE turns (each truncation = dead turn + nudge).
