# gate_lib.sh — SPEC (efficiency + safety primitives for the gate harness)

Status: DESIGN (spec only — no implementation shipped yet).
Audience: the build agent that will implement this, and any user-agent that
builds its own gate lanes/workers.

## Purpose (read first — the frame)

The dinomem **cron gate** (`cron_gate.sh`) is not for tuning dinomem's own crons.
It is a **harness dinomem hands to the installing user's agent** so that whatever
*that* agent builds (a periodic job, a background worker, a self-made tool) comes
out **cost-efficient by default** — a cheap zero-LLM check decides, the expensive
LLM worker wakes only on a real signal.

This spec extends that harness from **cost-only** to **efficient + safe across
every resource axis**, so the user's agent's builds are also RAM/disk/CPU-aware,
scalable-by-construction, and cannot brick OpenClaw or the OS.

Everyday user experience is UNCHANGED and invisible. The new surface appears
ONLY when the user's agent chooses to build a new lane/worker.

## REPO PLACEMENT (RESOLVED: build in BASE only)  [READ]

The router (`cron_gate.sh`) lives in BASE; improver/advancer/deleter/ideator
live in NEURON. Neuron installs ON TOP OF base (overwrite-same-named rule), so
base's files are ALWAYS present on a neuron box. Therefore:

- **Build the ENTIRE lib in BASE, one copy: `scripts/lib/gate_lib.sh`**
  (Layers A + B + C + D). Neuron inherits it for free — the neuron installer
  adds NOTHING. Single source of truth, no duplication, no drift.
- Base currently INLINES the work-once guard inside `check_daily_notes.sh`.
  Layer A is that logic FACTORED OUT into base's `gate_lib.sh`; base's own
  `check_daily_notes.sh` then sources it (inline copy removed).
- **NEURON already extracted a version** of this as `scripts/_gate_refire_guard.sh`
  (`refire_should_fire`), SHARED by check_projects/improvable/deletable/ideator
  /daily_notes. Neuron's ONLY change: shrink `_gate_refire_guard.sh` to a THIN
  COMPAT-SHIM — `source` base's `gate_lib.sh` + alias
  `refire_should_fire = guard_composite`. Its 5 gates keep working, ZERO churn,
  and inherit B/C/D. That shim belongs in neuron because it is about neuron's
  own files; the PRIMITIVES stay in base.
- Neuron's WORKERS (improver/advancer) are CONSUMERS that wire B/C/D in.

Net: base owns the primitives (general, easy-install, everyone gets them);
neuron only shims its own guard file. Do NOT fork or copy the lib into neuron.

Status of Layer A: proven + in-use in neuron; being MOVED DOWN to base + shimmed.
NOT greenfield — continuation of a refactor neuron already began.
SPEC HOME: BASE `docs/gate_lib_SPEC.md` (this file).

## Design law (strict floor, open ceiling)

Enforce the **outcome**, not the **method**.

- HARD FLOOR (non-negotiable): (1) never wake the paid LLM on no signal;
  (2) never re-do unchanged work; (3) never brick OpenClaw/OS (schema-safe
  writes only). Bound to the **installed binary's validator** => version-matched
  by construction, works offline.
- OPEN CEILING (dynamic): HOW a lane meets the floor is the agent's choice. Each
  primitive ships a written CONTRACT + one satisfying default impl. A better
  method is welcome IF it passes the floor's TEST. Better defaults ship as `_v2`;
  old names preserved. The paved road moves; it never cages.

Every guarantee is backed by a TEST (test-don't-assume). "Looks right" has
shipped silent bugs here before (see BUG HISTORY). A replacement is proven by
passing the same test, not asserted.

## Cross-cutting contracts (apply to EVERY function)

- fail-open: any internal error -> safe default; the gate tick still `exit 0`.
  A broken primitive can never brick a tick.
- zero-LLM: Layers A-B-C never call a model. Only `trigger*` wakes the paid worker.
- versioned: each fn carries `# vN`; better default -> `name_v2`, old name kept.
- offline-proof + version-matched for the safety layer (D): bound to local
  installed validator, never to "latest" docs.

---

## Layer A — waste-floor (work-once + debounce)  [ABSORB EXISTING]

```
guard_by_hash <state_file> <input...>
  Exit 0 (run) iff aggregate content-hash of <input> differs from <state_file>;
  else exit 1 (skip). Hash EXCLUDES volatile churn (e.g. claimed_by/claimed_at)
  — that exclusion is the v1 self-perpetuating-loop fix. Persists new hash on run.

guard_by_interval <state_file> <min_secs>
  Exit 0 iff now - last_run >= min_secs; else exit 1. Stamps on run.

guard_composite <state_file> <min_secs> <input...>
  = guard_by_hash AND guard_by_interval over one state file.
  This IS today's refire_should_fire(); keep that name as an alias for callers
  already sourcing _gate_refire_guard.sh (back-compat, zero churn).
```
FLOOR MET: "never re-do unchanged work" + rate floor. Kills the v0
fire-every-tick bug and the v1 claim-mtime loop.

## Layer B — resource sensors (adaptive input; READ-ONLY)

```
sensor_ram_mb                 -> available MB
sensor_disk_free_pct <path>   -> int % free on path's fs
sensor_load_ratio             -> loadavg / nproc (1.0 = saturated)
sensor_cores                  -> usable core count
```
CONTRACT (all): pure stdout number, `exit 0` ALWAYS, fail-open to a SAFE DEFAULT
on unknown platform (BusyBox/macOS: no /proc) — never empty, never crash the gate.
Sensors DECIDE NOTHING; the caller decides. Portability MUST be tested on the
actual target host (df -P columns, /proc presence differ).

## Layer C — adaptive trigger (decide HOW, not just WHETHER)

```
trigger <label> <jobid>              (UNCHANGED from today)
  Fire disabled worker via `openclaw cron run`, swallow failure, record label.

trigger_p <label> <jobid> <KEY=***
  Like trigger, but threads KEY=*** env into the worker so it adapts (batch
  size, parallelism). Back-compat: old lanes keep calling plain trigger.
  MUST be tested against crond (strips interactive env — known install.sh footgun).

pick_batch <ram_mb>                  -> a batch size for this box (tiered; override-able)
defer_if_busy <load_ratio> <ceiling>
  Exit 1 (skip THIS tick) if load > ceiling — CPU backpressure.
  CONTRACT: MUST NOT defer forever. Pair with guard_by_interval as a STARVATION
  floor so a saturated box still eventually runs. (This is the v1 loop-trap guard
  applied to load — the exact failure class this repo has already been bitten by.)
```

## Layer D — safety-floor (version-matched, offline-proof, UNBYPASSABLE)

```
safe_config_write <json5_patch>
  Writes config ONLY via the installed binary's validated path
  (openclaw config patch/set). REFUSES raw openclaw.json edits. Invalid -> no
  write, non-zero, reason to stderr. Runs `openclaw config validate` after.
  Bound to the LOCAL installed validator => version-matched by construction,
  works offline. THE ONE PRIMITIVE THE AGENT MAY NOT REPLACE.

schema_field_ok <dotted.path>
  Consult LOCAL `config.schema.lookup` (version-matched) to confirm a field
  exists for THIS install before use. Offline-proof. NEVER trusts "latest" docs.

register_worker <name> <schedule|disabled> <payload>
  Register/update a cron via MERGE-NOT-CLOBBER, agent-scoped (wraps install.sh
  upsert_cron logic). Never rewrites another agent's crontab lines.

docs_hint <topic>            ENRICHMENT ONLY — never a gate
  Guidance from VERSION-PINNED docs (installed version, not latest); local doc
  bundle as fallback; total failure -> empty string, caller proceeds. Output is
  advice, never authority. Cannot block a write.
```

### Why local-first for safety (the online/offline resolution)

The safety GATE must be bound to something version-matched to the INSTALLED
OpenClaw. The installed binary's validator is version-matched BY CONSTRUCTION and
works offline — strictly better than "latest" docs, which can be too-new-and-wrong
for an older install. Rule:
- SAFETY GATE: local installed validator (unbypassable, offline, version-matched).
- GUIDANCE: version-pinned docs, online to refresh, local bundle fallback. Never
  the authority for a write.

---

## Who benefits (improver / advancer / deleter / ideator + user builds)

Neuron's own workers get the new layers FOR FREE, same way they already share
Layer A:

- Advancer (`check_projects.sh`): biggest D beneficiary (advances steps that can
  be external/destructive -> `safe_config_write`); C `defer_if_busy` so it won't
  advance a heavy build on a saturated box.
- Improver (`check_improvable.sh`): B sensors size its re-validation batch to RAM;
  keeps A (already has it).
- Deleter / Ideator: keep A; inherit fail-open + versioning hygiene.
- The user's OWN new lane: 3 lines, inherits all four layers (see below).

## Target ergonomics — a user-agent lane AFTER this exists

```
source "$SCRIPTS/lib/gate_lib.sh"
if [ -n "$MY_JOB_ID" ] && guard_composite "$STATE/myjob" 3600 "$DATA"/*.md; then
  trigger_p "myjob" "$MY_JOB_ID" "DINOMEM_BATCH=$(pick_batch "$(sensor_ram_mb)")"
fi
```
Three lines -> inherits work-once + debounce + host-fit batch + fail-open +
zero-LLM. Config writes (if any) go through `safe_config_write` (unbypassable).
No bug re-derivation.

## Required tests (ship with the lib — floor is a TEST, not a claim)

`test/gate_lib_test.sh` MUST assert:
1. guard_by_hash: 5 ticks, no content change -> 0 fires. Change body -> fires once.
2. guard_by_interval: suppresses within floor; passes after floor elapses.
3. guard_composite alias == current refire_should_fire behavior (regression lock).
4. defer_if_busy: high load defers, BUT interval floor still eventually fires
   (no starvation).
5. trigger_p: env actually reaches the worker under crond-like env stripping.
6. safe_config_write: rejects a knowingly-bad patch, writes nothing, leaves
   `openclaw config validate` GREEN.
7. sensors: return SAFE DEFAULTS on a faked unknown platform (no /proc), gate
   still exit 0.

## BUG HISTORY (promoted from code comments — do not re-ship)

- v0: gate fired on bare note EXISTENCE -> LLM woke every */15 tick (96x/day) to
  emit NO_REPLY. Fix: only fire on real signal.
- v1: gated on note MTIME vs last_run stamp -> SELF-PERPETUATING LOOP: the
  open-notes bootstrap hook + the worker both refresh `claimed_at:`, bumping mtime
  after the stamp, so "changed" was always true -> fired forever. Fix: hash the
  BODY, EXCLUDING claim lines (Layer A does this).
- Lesson encoded as law: every floor is a TEST. A "looks right" gate has shipped
  a silent bug here twice. Replacements prove themselves against the test above.
```
