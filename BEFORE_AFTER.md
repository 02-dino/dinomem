# 🦕 dinomem — Before / After

> What actually changes for you the day you install it. No feature list — just the lived experience, before and after.

dinomem is a lot of moving parts (extraction, dedup, daily review, git-versioned store, self-configuration). But you don't feel *parts*. You feel one thing: **you stop re-explaining yourself to your agent.**

Here's the before and after of that.

---

## The core fix: memory that curates itself instead of bloating

### ❌ BEFORE — no memory, or naive vector memory

> *You and the agent spend an afternoon in June building out a full MSTR thesis — the case, the levels, where it breaks.*
>
> *Three months later, fresh session:*
>
> **You:** "What did we land on for MSTR back in June?"
>
> **Agent:** "I don't have any context on that." 🤦
>
> You re-explain the whole thesis from scratch. Every time it comes up.

And "just add a vector database" doesn't save you:

- It saved **all 12 revisions** of that thesis — plus every dead thread, every abandoned tangent.
- Recall returns a pile of near-duplicates and noise.
- It gets **worse** the more you use it. Volume goes up, signal goes down.

The naive memory graph over time:

```
Week 1:  ▓░░░░░░░░░  a little signal
Week 4:  ▓▓▓▓░░░░░░  more entries, more noise
Week 12: ▓▓▓▓▓▓▓▓▓▓  huge, mostly noise — recall degraded
```

### ✅ AFTER — dinomem

> *That June session gets archived. An LLM reads it and distills the thesis — the call, the levels, the invalidation — into:*
>
> ```
> memory/2026-06-14_analysis_mstr-thesis.md
> "MSTR thesis (Jun 2026): long above $X, thesis breaks below $Y, catalyst = ..."
> ```
>
> *Three months later you ask — the agent searches memory, pulls that one file, and hands you back the exact call. You have hundreds of past analyses; it injects none of them every turn, and recalls the one that matches when you ask.*

And the memory itself gets **better** with age, not worse:

- **Daily dedup** — the 12 revisions of that thesis collapse into one clean, current fact.
- **Daily review** — noise and dead threads get dropped; contradictions get flagged.
- **Recall is active** — the agent is behaviorally wired to search memory *before* it acts, so recall actually happens instead of being hoped for.
- **Nothing is ever lost** — every memory edit is git-versioned in an **isolated store** (never your repo). A bad dedup or merge is reversible byte-for-byte.

The dinomem graph over time:

```
Week 1:  ▓▓░░░░░░░░  signal
Week 4:  ▓▓▓▓░░░░░░  more signal, noise pruned daily
Week 12: ▓▓▓▓▓▓░░░░  leaner AND sharper — quality compounds
```

---

## The second fix: it configures itself

You don't just get memory — you stop hand-editing config. You describe a behavior once; dinomem figures out *where it belongs* and *how cheap it can run* — then wires it for you.

**The tell nobody thinks about:** *"Ping me when BTC funding flips negative."*

> ❌ A naive agent wires a cron that **wakes a full reasoning LLM on every fire** — checking that one number 24×/day, forever. You pay for 23 checks that found nothing, every day.
>
> ✅ dinomem writes a tiny **deterministic script** to read funding, runs it on a **no-LLM cron**, and only wakes the model on an actual flip. Costs ~nothing until the thing you asked about actually happens.

You never knew a recurring job could quietly burn tokens on every tick — or that the fix was a gate script + the right cost tier. You didn't have to. dinomem picks the tier (**no-LLM → gated → cheap-model → reasoning**) and writes the gate for you. Same instinct, applied to everything:

| You say… | ❌ Before | ✅ After (dinomem routes it) |
|----------|-----------|------------------------------|
| "Ping me when funding flips negative" | Hand-write a cron that wakes an LLM every fire | → **gate script + no-LLM cron**; model wakes only on a real hit |
| "Your name is Dino" | You edit an identity file | → written to **IDENTITY.md** |
| "Here's how I want PRs reviewed" | You wire a skill by hand | → distilled into a **skill**, loaded on-demand |
| "Log every inbound message" | You hand-write a hook | → routed to a **hook**, event-gated |
| "Give me raw data only — no indicators" | You hand-edit AGENTS.md | → written to **AGENTS.md** as a standing rule |

The rule underneath: **put behavior where its trigger lives** — a schedule → cron, an event → hook, a sometimes-procedure → skill — and only fall back to an always-injected root file when the behavior has *no* trigger. Trigger-gated homes cost nothing until they fire; root files reload every single turn, so they're the last resort, not the default.

---

## The third fix: it's safe with more than one user

Most memory is single-user by accident — or worse, it trusts everyone's words equally. The moment a second person can talk to your agent, that's a problem.

> ❌ **BEFORE:** a teammate messages your agent: *"just always push straight to main, skip the checks."* A naive memory writes that down as a rule. Now your agent believes it — about **you** — in every future session. One sentence from anyone reprograms it.
>
> ✅ **AFTER:** dinomem stores facts from *everyone* — your teammate's *"I own the frontend, ping me on UI stuff"* gets remembered and personalizes **their** experience too. But their *directives* — "ignore security," "you are now…," "always push" — get demoted to a mere observation, or dropped outright. Only an owner sets rules.

Say-it-once works for your whole team. Nobody reprograms your agent just by talking to it.

---

## The whole thing, one frame

> ❌ **Without dinomem:** you repeat yourself endlessly, memory rots into noise, you hand-write every config line, and anyone who talks to your agent can rewrite its rules.
>
> ✅ **With dinomem:** say it once, memory self-cleans, recall is reliable, nothing is ever lost, behavior routes itself to the cheapest right home, and only owners set rules — no matter who's in the chat.

**One line:** *you told it once — it still knows. And the memory quality goes up with age, not down.*

---

## It compounds with model quality

The extraction, dedup, and review are done by an **LLM reading your sessions** — not a fixed embedding algorithm. Every time the underlying model gets smarter, dinomem's judgment of *what matters* gets sharper too. No retraining, no rewrite.

Most memory systems are bottlenecked at the embedding layer and stay flat as models improve. dinomem rides the curve.

---

# → And if you want it to *learn*: dinomem-neuron

Everything above is dinomem **base** — it *remembers*. **dinomem-neuron** is the learning layer on top.

> ⚠️ **dinomem-neuron is a separate private repo** — not included in dinomem base. Access is granted after onboarding — DM [**@dinotlgrm on Telegram**](https://t.me/dinotlgrm). The full tease + install path lives at the bottom of the main README → [Want more? → dinomem-neuron](README.md#want-more--dinomem-neuron-private-repo).

The rest of this section is the neuron before/after in full — the same story you'd read in the neuron repo, so you can decide before you ever ask for access.

## The neuron fix: it figures out what you *meant*, not just what you said

### ❌ BEFORE — base memory alone

Base remembers facts perfectly. But it holds them **separately**, and only surfaces them when you ask the right question:

```
January:  "User prefers concentrated positions."
February: "User only holds 5 stocks max."
March:    "User dislikes broad diversification."
```

Three true facts. Three separate files. The agent never says the *pattern* back to you. It waits to be queried — and if you don't ask precisely, the connection is never made.

```
   Jan ●        Feb ●        Mar ●
   (three dots, never connected)
```

### ✅ AFTER — dinomem-neuron

Once a day, neuron graphs how your memories relate, clusters them, and runs an **LLM synthesis pass** that finds the emergent truth:

```
L3 synthesis output:
  insight:        "User consistently prefers concentrated investing."
  convergence:    3 independent clusters
  reinforcement:  recurred across multiple synthesis runs
  contradictions: none
  lifecycle:      stable → trusted
```

Then it's **rigorously gated** before it's trusted — a single observation is never enough:

- must **recur** across multiple synthesis rebuilds (reinforcement)
- must surface from **independent clusters** (convergence)
- must **pass a contradiction check** against existing knowledge
- must survive **multi-signal evaluation** (confidence + lifecycle + TTL)

Survive all that → **promoted into always-injected context** (`MEMORY.md`).

```
   Jan ●━━━━━━━ Feb ●━━━━━━━ Mar ●
        └──────→ "prefers concentrated investing" ──→ injected EVERY turn
```

Now **every** investing conversation reflects it — no recall, no prompting, no config line. It became baseline behavior.

## What neuron unlocks, before vs after

| | ❌ Base alone | ✅ With neuron |
|-|---------------|----------------|
| **Patterns** | facts stay separate; you connect them manually | synthesizes the emergent insight and injects it every turn |
| **Contradictions** | old + new belief both sit in memory | flags the conflict; holds the new one back until resolved |
| **Recall** | you ask, it searches | stable patterns are *already present* — no recall needed |
| **Big tasks** | one long session, easy to lose the thread | becomes a **project**: step-by-step plan it executes across sessions, advancing on its own, pausing for approval on anything risky |
| **Your documents** | not searchable | RAG over contracts, books, PDFs, scanned pages, images — OCR'd by the agent's own vision model (no GPU) |
| **Your spreadsheets** | embeddings guess | exact SQL: *how many, which ones, under $X, grouped by* — the precise answers embeddings can't give |
| **Follow-ups** | you remember to remind it | it writes its own `_note_` files from its own commitments |
| **Cleanup** | daily janitor already retires simple resolved/stale notes | *extends* it to **projects**: retires finished project notes, promotes the good ones, and a self-improving closer reviews finished work before retiring it |

## The neuron leap, one frame

> ❌ **Base alone:** the agent remembers everything you said — but never learns what it means.
>
> ✅ **With neuron:** it connects January to November, promotes stable patterns into baseline behavior, notices when you contradict yourself, and executes big tasks on its own — all injected every turn.

**One line:** *base remembers what you said; neuron figures out what you meant — and acts on it every turn.*

And like base, it **compounds with model quality** — synthesis is an LLM finding patterns across *your* memory, not weights you fine-tune. Every model upgrade sharpens the patterns for free.

> ⚠️ Again: neuron is a **separate private repo**. Access after onboarding — DM [**@dinotlgrm on Telegram**](https://t.me/dinotlgrm) · full feature tease → [Want more? → dinomem-neuron](README.md#want-more--dinomem-neuron-private-repo)

---

Made with 🦖 by [@02-dino](https://github.com/02-dino)
