---
name: auto-improve
description: Turn one detected session candidate into ONE reviewable self-improvement proposal. Read this when the improvement cron fires, or when the owner asks for an improvement pass. Covers the candidate lifecycle, surface routing, the lessons-not-logs writing rule, and the pending-gate contract.
---
# Auto-improve (dinomem)

The AUTOMATIC half of the build stack. The manual path is unchanged: this adds a
lane that notices a lesson on its own and drafts it, but it can never ship one —
every write waits for the owner at the `route.py pending` gate.

A candidate is a session that structurally looked like it contained a lesson.
L1 already extracted a <=6 KB excerpt, so the transcript you are given IS the
evidence. Do not open the session file: raw spans reach 1.9 MB, past a context
window, which is why the excerpt exists.

## The pass (run in order, stop early when told to)

1. **Claim one candidate**
   ```bash
   python3 procedures/improvement_propose.py next
   ```
   Exit 1 = empty queue -> **STOP, say nothing, cost nothing**. This is the
   normal outcome on most days and is not a failure.

2. **Decide: is there a real lesson here?**
   The detector optimises for recall, so most candidates are ordinary work that
   merely looked busy. Dropping is the expected majority outcome.
   A lesson exists only if a FUTURE session would act differently because of it.
   - repeated retries against one target -> what finally worked, and WHY
   - a user correction the agent then obeyed -> the rule behind the correction
   - a multi-step workflow worth repeating -> the order and the pitfalls
   No lesson?
   ```bash
   python3 procedures/improvement_propose.py drop <id> --why "<one line>"
   ```
   then STOP. A wrong proposal costs the owner more than a missed one.

3. **Pick the surface — never guess**
   ```bash
   python3 tools/route.py classify
   ```
   Apply the schema it prints. The fork that matters: does this RECORD
   something (fact/outcome/todo -> memory) or GOVERN future turns (rule/SOP/
   tool spec/persona -> root)? Both -> split, and propose only the stronger half
   this pass. Root is last by cost and must earn it; a schedule -> cron, an
   event -> hook, an on-demand procedure -> skill.

4. **Check it is not already covered**
   ```bash
   python3 tools/route.py verify <surface> "<distinctive phrase>" [--file F]
   ```
   Exit 0 means the behaviour already exists -> `drop` it. Never propose a rule
   the agent already has; a duplicate rule is pure context cost.

5. **Write the draft to the pending gate**
   Build `kb/improvement_pending/<id>.json`:
   ```json
   {
     "id": "<candidate id>",
     "status": "pending",
     "created_at": <epoch>,
     "surface": "memory|skill|cron|hook|root",
     "title": "<one line, what changes>",
     "argument": "<why, citing what happened in the excerpt>",
     "evidence_ref": "<candidate id>",
     "action": { "...": "see below" }
   }
   ```
   - memory -> `{"path": "memory/_pin_<slug>.md", "content": "<full file>"}`
   - every other surface -> `{"argv": [...]}` for its leaf writer
     (`skill_tool.py` / `cron_tool.py` / `hook_tool.py` / `config_tool.py`)
   Never pass `--confirmed`: the owner's approval IS the confirmation, and the
   gate refuses a pre-confirmed proposal.

6. **Lint your own draft before filing it**
   ```bash
   python3 tools/route.py lint <file-or-->
   ```
   Fix what it flags. See the writing rule below.

7. **Close the candidate**
   ```bash
   python3 procedures/improvement_propose.py done <id>
   ```

## How to write the `argument` (the owner reads this, once)

The argument is the only thing standing between a proposal and a yes/no. Write
it the way you would explain it out loud to someone who was not there — in the
owner's own language, plain words, no internal vocabulary. An owner who has to
decode a proposal will eventually stop reading proposals, and a gate nobody
reads approves everything by default.

**Language: match the owner's.** Write the argument in whatever language the
owner actually speaks in their sessions (check the excerpt). A proposal in a
language the reader has to translate gets skimmed, and a skimmed proposal gets
waved through. Tool names, file paths and commands stay as-is; the prose around
them does not.

Shape it like this:

1. **What happened**, as a short story. Concrete, no jargon.
2. **Why it will keep happening**, the mechanism in one plain sentence.
3. **A before/after sketch** — the same moment, handled the old way and the new
   way, side by side. Show it; do not explain it. The reader should feel the
   difference without being told what to conclude.
4. **The cost**, measured. Seconds, retries, tokens — a number they can check.

Name tools and commands only where the reader needs them to verify the claim.
Everything else is plain language.

The before/after sketch is the part that does the convincing, so make it a
scene, not a summary. Two short columns of what the agent actually says or does
at the same moment — old way on the left, new way on the right — and let the
reader draw the conclusion themselves. If you find yourself writing "as you can
see, the new way is better", delete that line: the sketch failed and no amount
of explaining will fix it.

## The writing rule: lessons, not logs

A pitfall is a **generalisable rule plus one clause of WHY (the mechanism)**,
attached to the step it affects, stated once.

Not skill content: incident narration, dates, PR/issue numbers, quoted chat, or
a blow-by-blow `then I tried X and it failed`. The rule has to stand without the
story behind it — a future reader needs the rule, not the autopsy.

Also never restate what is already loaded every turn (AGENTS.md, tool schemas).
A rule that is already in context costs tokens twice and teaches nothing.

### Examples: generous in the argument, rationed in the rule

These two texts have opposite economics, so they get opposite budgets.

- **argument** — read once, by a human, then discarded. Cost is effectively
  zero, so a before/after sketch is always worth it.
- **rule body on a root file** — injected into every turn of every session
  forever. An example there is paid for thousands of times.

So: at most ONE line of example in a root rule, and only when the rule is about
the SHAPE of a response (what to say instead of what). A rule whose meaning is
already obvious from its own wording gets no example at all. Skill bodies are
loaded on demand, so they can afford more.

## Hard limits

- **You never write a live surface in this pass.** Only `kb/improvement_pending/`.
  Approval and application belong to the owner via `route.py approve`.
- **One candidate per run.** Several lessons in one session -> propose the
  strongest; the rest resurface if they recur, and recurrence is evidence.
- **Code in a proposal** (a gate script, a hook handler) must follow
  `build-quality` before it is drafted, and anything landed later is verified
  with `scripts/verify.sh` + `route.py dup` by whoever approves it.
- **No lesson is the normal answer.** An empty queue and a dropped candidate are
  both successful runs.
