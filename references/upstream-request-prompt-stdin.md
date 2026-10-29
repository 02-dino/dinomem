# Upstream request: accept a prompt from stdin / a file on `openclaw infer model run`

Status: DRAFTED 2026-10-29, not yet filed.
Target: https://github.com/openclaw/openclaw/issues
Affects: dinomem + dinomem-neuron (6 call sites), and any integrator that
shells out to the inference CLI with machine-generated input.

This file is the artifact for item (c) of the argv/E2BIG work. It is kept in the
repo so the request is reviewable before it is posted publicly, and so the
reasoning survives whether or not upstream accepts it.

---

## Title

`infer model run`: allow the prompt to come from stdin or a file (argv caps it at 128 KB)

## Body

**Summary**

`openclaw infer model run` (and its `openclaw capability model run` alias) accepts
the prompt only as a single argv value:

```js
command("run")
  .description("Run a one-shot model turn")
  .requiredOption("--prompt <text>", "Prompt text")
```

Linux caps one argv *string* at `MAX_ARG_STRLEN` = 32 pages = **131072 bytes**.
That is a separate limit from the much larger total `ARG_MAX`, so it cannot be
raised with `ulimit`, a bigger `ARG_MAX`, or any host tuning. Any caller whose
prompt grows past ~128 KB gets `OSError: [Errno 7] Argument list too long`
before the process is even spawned.

**Measured** (same box, `ARG_MAX` = 2097152):

| single argv value | result |
| --- | --- |
| 100,000 bytes | ok |
| 131,000 bytes | ok |
| 131,072 bytes | `errno 7` |
| 510,672 bytes | `errno 7` |

**Why it matters in practice**

This is a function of *input size*, so it is not a rare edge: any integration
that summarizes a growing corpus crosses it eventually. In our case a workspace
reached a 510,672-char prompt (~3.9x over the ceiling). The failure is also
quiet from the user's side — the call never reaches a provider, so it reads as
"the model call failed" rather than "the argument never fit". On two agents the
affected job failed 14 of 16 and 16 of 20 runs over several weeks before anyone
noticed, because each run degraded to a non-LLM fallback path instead of
erroring loudly.

**Why we are asking upstream rather than just working around it**

We have bounded our own input, which fixes our symptom. But bounding means
*dropping real content*, and some callers genuinely cannot: a reviewer that
rewrites a file must see the whole file, since a truncated view would silently
discard whatever it did not see. For those callers there is no safe local
workaround short of not using the CLI.

**Proposed change**

Accept the prompt through a channel that is not argv. Either form solves it:

- `--prompt-file <path>` — read the prompt from a file; mutually exclusive with `--prompt`.
- `--stdin` — read the prompt from standard input; mutually exclusive with `--prompt`.

`--stdin` already exists as an idiom elsewhere in this CLI (`config patch --stdin`,
`approvals set --stdin`, exec-approvals, git), so this would be consistent with
the existing surface rather than a new concept. Keeping `--prompt` unchanged
makes the change fully backward compatible.

The same ceiling applies to the other prompt-taking subcommands
(`image generate`, `video generate`, ...), which use the same
`requiredOption("--prompt <text>")` shape, though they are far less likely to
hit it in practice.

**Versions checked**

- 2026.7.1-2 — `dist/capability-cli-*.js`: `requiredOption("--prompt <text>")`, no stdin/file path.
- 2026.10.8 (latest at time of writing) — `dist/model-*.mjs`: identical
  `requiredOption("--prompt <text>")`. The only added flag versus 2026.7.1 is
  `--agent <id>`. No stdin/`--prompt-file` option; `docs/cli/infer.md` in the
  package and the live docs site do not mention one.

So this is still present on latest, and upgrading does not resolve it.

---

## Notes for whoever files this

- Check for an existing issue first; this is a plausible duplicate.
- The measured table above is the strongest part. Keep it.
- Do not paste any workspace paths, agent ids, or config contents into the
  public issue — everything above is deliberately free of them.
