/**
 * dinomem-recall-gate — MID-SESSION recall enforcement (the piece bootstrap can't do).
 *
 * PROBLEM (dino, 2026-07-25): the dinomem-open-notes bootstrap hook injects the
 * M2/M3 recall gate ONCE, at agent:bootstrap. Mid-session (turn 5, turn 20...)
 * nothing re-injects, so recall rests on the model's own discipline — the exact
 * "plea to the model" we were mechanizing away.
 *
 * DESIGN (v2, LANGUAGE-AGNOSTIC): earlier drafts parsed the USER MESSAGE for
 * "named entity / continuation" keywords. That was proven English-only — a
 * multilingual test scored 1/5 on Indonesian, Spanish, and Chinese back-
 * references (all accidental hits on literal tool names). You cannot wordlist
 * across every language an installer might type in. So we STOPPED parsing the
 * message entirely.
 *
 * The gate now fires on the DANGER, not the message: if the model reaches for
 * the FILESYSTEM/SHELL (exec/read/grep/glob) *cold* — i.e. WITHOUT having run any
 * recall tool yet this turn — it gets ONE block telling it to recall first. Zero
 * language understanding; can't be defeated by typing in another language. A
 * false positive (a genuinely-fresh fs reach) costs exactly one cheap recall
 * call — the safe direction. A false negative is silent amnesia, the thing this
 * exists to kill.
 *
 * PER-TIER TOOLING: recallTools/fsTools come from config, so base dinomem
 * (memory_search/memory_get only) and neuron (+ session_search/graph_search/
 * docs_search/data_query/semantic_search) each ship a config matching their
 * actual tool set. The CODE is tier-agnostic; only the shipped config differs.
 *
 * WHY before_tool_call: internal message:received hooks cannot inject into model
 * context; only bootstrap + tool-call interception can steer a live turn. This is
 * the only mechanized mid-session lever.
 *
 * FAIL-OPEN: any error never blocks a tool. A recall-nudge bug must never brick
 * the agent. Owner-only concern — dinotrust-enforce owns the security floor; this
 * is purely a recall nudge.
 */

// Import path MUST match the runtime's resolvable SDK entry. The working hooks
// (dinotrust-enforce, note-claim-oncreate) use "openclaw/plugin-sdk/plugin-entry".
// The scoped "@openclaw/plugin-sdk" form silently failed to load here (2026-08-02
// live test: before_tool_call handler never fired -> gate was dead), matching the
// 'definePluginEntry is not a function' report. Aligned to the working path.
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import fs from "node:fs";
import path from "node:path";

// ── Config (overridable via plugins.entries["dinomem-recall-gate"].config) ──
type Cfg = {
  enforce: boolean;        // false => dry-run: never actually block
  agentFilter: string;     // only act on sessionKeys containing this; "" = all
  recallTools: string[];   // tools that COUNT as "recall done this turn" (TIER-SPECIFIC)
  fsTools: string[];       // fs/exec tools whose COLD use triggers the nudge
  cooldownTurns: number;   // min turns between fires (avoid nagging on repeated ignores)
  // ── TIER B: hot-zone WRITE re-arm (2026-08-02) ──
  // A recall latched EARLIER in the turn silences the cold-fs nudge; but a
  // write/edit/apply_patch to a MECHANISM path is a distinct danger (building on
  // a system with failure history -- e.g. the Generator-retry / 12.4M-blowup near-
  // miss) that a stale general recall must NOT cover. So a hot-zone write requires
  // a FRESH recall this turn even if recallDone is already latched.
  writeTools: string[];      // write/edit/apply_patch (+aliases)
  hotZoneGlobs: string[];    // substrings that mark a mechanism path (scripts/, tools/, config)
  // ── TIER C: fuzzy-recall route enforcement (2026-09-02) ──
  // NEURON-ONLY. Neuron ships a single fuzzy-recall "door" (hybrid_recall) that fuses
  // + reranks the individual legs. But the model's habit is to reach the raw
  // gatedTool (memory_search) reflex instead of routing through the door. Tier C
  // routes that reflex to the door. OFF by default (routeEnforce:false) so BASE
  // users -- who have NO hybrid_recall -- are never blocked reaching a tool they
  // lack. Neuron's config turns it ON. Soft-nudge once, then hard-block on repeat.
  // EXEMPT: gatedTool is allowed once hybrid_recall has run this turn, because
  // hybrid_recall itself calls memory_search natively for --external-hits folding;
  // blocking that would make the door self-block into an infinite stall.
  hotZoneExclude: string[];  // substrings that DISQUALIFY (memory/, gate's own dir) -> loop-safe
  routeEnforce: boolean;     // TIER C master switch (default false = base-safe, neuron turns on)
  routeTool: string;         // the fuzzy "door" to route to (hybrid_recall)
  gatedTool: string;         // the raw tool whose reflex we redirect (memory_search)
  // CAPABILITY GATE (2026-10-20): routeTool is a NEURON CLI living in the agent's
  // OWN workspace, so "is Tier C safe here?" is a per-agent question, not a global
  // one. Without this, widening agentFilter to "" (all agents) would hard-block
  // gatedTool in workspaces that have no routeTool at all -> recall goes to ZERO
  // there. Probing for the file makes the gate self-maintaining: a workspace that
  // installs neuron later is covered automatically, one that never does is never
  // gated. Empty string disables the probe (always gate).
  routeToolPath: string;     // workspace-relative path that must EXIST for Tier C to arm
  // ── TIER D: durable-surface nudge on memory writes (2026-10-18) ──
  // WHY: memory was winning by DEFAULT, not by merit. Every other surface trigger
  // in AGENTS.md reads "when USER implies...", while memory_pin also self-fires
  // ("...or a todo arises"), so an AGENT-noticed improvement had exactly ONE open
  // door and took it. A RULE parked in a _pin_ is retrieval-gated => never loaded
  // => the behaviour silently never happens. This tier asks the RECORD-vs-GOVERN
  // question at the moment of the write, which is the only moment it is cheap.
  // DELIBERATELY THE SOFTEST TIER (owner call, 2026-10-18: "jgn maksa banget gt,
  // kalo memang itu baiknya di _pin memory ya udah kesitu aj"): fires at most ONCE
  // per turn and NEVER escalates to a hard block, unlike Tier C. A legitimate pin
  // costs one re-issue; a misrouted rule costs a silently-dead behaviour. The
  // asymmetry is why this is a nudge and not a gate.
  surfaceNudge: boolean;     // TIER D master switch
  surfacePaths: string[];    // memory-write paths that should answer RECORD vs GOVERN first
  routeMarker: string;       // substring marking "a routing decision ran this turn"
};

function cfg(raw: any): Cfg {
  const c = raw ?? {};
  return {
    enforce: c.enforce !== false,
    agentFilter: typeof c.agentFilter === "string" ? c.agentFilter : "analyst",
    // Default = FULL neuron set. Base dinomem MUST override to [memory_search,
    // memory_get] in its shipped config, else the blockReason names tools a base
    // user lacks. The plugin never assumes a tier; the config declares it.
    recallTools: Array.isArray(c.recallTools) ? c.recallTools
      : ["memory_search", "memory_get", "graph_search", "session_search", "semantic_search", "docs_search", "data_query"],
    fsTools: Array.isArray(c.fsTools) ? c.fsTools : ["exec", "read", "grep", "glob"],
    cooldownTurns: Number.isFinite(c.cooldownTurns) ? c.cooldownTurns : 3,
    // Write-tier: tool-agnostic across tiers (write/edit/apply_patch exist
    // everywhere), so base and neuron share these defaults -- unlike recallTools.
    writeTools: Array.isArray(c.writeTools) ? c.writeTools
      : ["write", "edit", "apply_patch", "Write", "Edit", "NotebookEdit"],
    // GENERIC defaults for easy buyer install -- mechanism dirs + config files,
    // no analyst-specific paths. Buyers override for their own layout.
    hotZoneGlobs: Array.isArray(c.hotZoneGlobs) ? c.hotZoneGlobs
      : ["scripts/", "procedures/", "tools/", "openclaw.json", ".gen_control", "crontab"],
    // Loop-safety: NEVER gate writes to memory/ (the gate would fire on its own
    // memory notes -> self-perpetuating, the 2026-07-24 cron-gate-loop lesson),
    // nor the extension's own dir, nor backup/log churn.
    hotZoneExclude: Array.isArray(c.hotZoneExclude) ? c.hotZoneExclude
      : ["memory/", "extensions/dinomem-", ".backups/", "logs/"],
    // TIER C (fuzzy-recall route). OFF by default -> base users (no hybrid_recall)
    // are never gated. Neuron config sets routeEnforce:true.
    routeEnforce: c.routeEnforce === true,
    routeTool: typeof c.routeTool === "string" ? c.routeTool : "hybrid_recall",
    gatedTool: typeof c.gatedTool === "string" ? c.gatedTool : "memory_search",
    routeToolPath: typeof c.routeToolPath === "string" ? c.routeToolPath : "tools/hybrid_recall.py",
    // ON by default for BOTH tiers: unlike Tier C (which needs neuron's
    // hybrid_recall and so must stay off for base), route.py + memory/ ship with
    // base dinomem, so this is safe everywhere. Soft by construction.
    surfaceNudge: c.surfaceNudge !== false,
    surfacePaths: Array.isArray(c.surfacePaths) ? c.surfacePaths
      : ["memory/_pin_", "memory/_note_"],
    routeMarker: typeof c.routeMarker === "string" ? c.routeMarker : "route.py",
  };
}

// ── Tier C capability probe: does THIS agent's workspace actually HAVE routeTool? ──
// WHY: Tier C hard-blocks gatedTool and tells the model to use routeTool instead.
// routeTool is a NEURON CLI living in the agent's OWN workspace. Measured 2026-10-20
// on this box: 11 of 19 workspaces have tools/hybrid_recall.py, 8 do not. So opening
// agentFilter to "" (all agents) WITHOUT this probe would hard-block the only recall
// path those 8 have -> recall drops to ZERO there. Probing the file makes the gate
// self-maintaining: install neuron later and that lane arms itself; never install it
// and the lane is never gated. Fail-OPEN by construction (can't resolve => not armed).
//
// Workspace resolution mirrors dinomem-retrieval-log's resolver (same OpenClaw
// convention <OPENCLAW_DIR>/workspace-<agentId>), deliberately NOT imported: plugins
// install as independent dirs with no shared module, so a cross-plugin import would
// couple their install order. The probe target differs too (routeTool vs its logger).
const _capCache = new Map<string, boolean>();
function openclawRoots(): string[] {
  const roots: string[] = [];
  const push = (d?: string) => { if (d && !roots.includes(d)) roots.push(d); };
  push(process.env.OPENCLAW_DIR);
  push(process.env.OPENCLAW_HOME);
  push(path.join(process.env.HOME || "/root", ".openclaw"));
  push("/root/.openclaw");
  return roots;
}
function hasRouteTool(c: Cfg, agentId: string, ctx: any): boolean {
  if (!c.routeToolPath) return true;            // probe disabled -> always armed
  const key = `${agentId}|${c.routeToolPath}`;
  const hit = _capCache.get(key);
  if (hit !== undefined) return hit;
  const probe = (dir?: string | null): boolean => {
    try { return !!dir && fs.existsSync(path.join(dir, c.routeToolPath)); } catch { return false; }
  };
  let found = false;
  if (agentId) {
    for (const root of openclawRoots()) {
      // shared-dir layout (<root>/workspace-<id>) + per-instance layouts.
      if (probe(path.join(root, `workspace-${agentId}`)) ||
          probe(path.join(`${root}-${agentId}`, `workspace-${agentId}`)) ||
          probe(`${root}-${agentId}`)) { found = true; break; }
    }
  }
  if (!found) {                                  // runtimes that expose cwd but no agentId
    const cwd = ctx?.cwd ?? ctx?.workspace ?? ctx?.workspaceDir;
    if (typeof cwd === "string" && cwd) found = probe(cwd);
  }
  _capCache.set(key, found);
  return found;
}

// Pull plausible target file paths out of a write tool's params. Mirrors
// note-claim-oncreate/extractTargetPaths: tools name the field differently
// (path / file_path / filePath / filename); apply_patch encodes paths in the
// patch body ("*** Update File: <p>"). Also honors host-derived path hints.
function extractTargetPaths(event: any): string[] {
  const out: string[] = [];
  const push = (v: unknown) => { if (typeof v === "string" && v.length > 0) out.push(v); };
  const params: any = event?.toolArgs ?? event?.args ?? event?.input ?? event?.params ?? event?.arguments;
  if (params) {
    push(params.path);
    push(params.file_path);
    push(params.filePath);
    push(params.filename);
    const patch = params.input ?? params.patch ?? params.content;
    if (typeof patch === "string" && /\*\*\* (Add|Update|Delete) File:/.test(patch)) {
      const re = /\*\*\* (?:Add|Update|Delete) File:\s*(.+)/g;
      let m: RegExpExecArray | null;
      while ((m = re.exec(patch)) !== null) push(m[1].trim());
    }
  }
  const derived = event?.derivedPaths ?? event?.targetPaths;
  if (Array.isArray(derived)) for (const p of derived) push(p);
  return out;
}

// Hot-zone = at least one target path matches a glob AND matches NO exclude.
// No path info -> NOT hot (fail-open: we never block a write we can't scope).
function pathIsHotZone(paths: string[], c: Cfg): boolean {
  for (const p of paths) {
    const s = String(p || "");
    if (c.hotZoneExclude.some((x) => s.includes(x))) continue;
    if (c.hotZoneGlobs.some((g) => s.includes(g))) return true;
  }
  return false;
}

// ── Per-turn state (in-memory). A "turn" = one user message + its tool calls. ──
// Keyed by sessionKey. We do NOT inspect message CONTENT for meaning (that was the
// English-only trap) — we only fingerprint it to detect turn boundaries.
type TurnState = {
  turnId: string;       // fingerprint of the current user message (boundary detection only)
  turnIndex: number;    // monotonic counter of turns in this session
  recallDone: boolean;  // a recallTool ran during this turn
  firedTurn: number;    // turnIndex of last cold-fs fire (-Infinity if never) -> cooldown
  writeFiredTurn: number; // turnIndex of last hot-zone-write fire -> independent cooldown
  routeFiredTurn: number;  // turnIndex of last TIER-C route soft-nudge -> soft-then-hard within turn
  routeDecisionDone: boolean; // TIER D: a routing decision (route.py) ran this turn
  surfaceFiredTurn: number;   // turnIndex of last TIER-D nudge -> once per turn, never escalates
};
const _state = new Map<string, TurnState>();

function userMsgOf(event: any, ctx: any): string {
  return String(
    ctx?.userMessage ?? ctx?.messageText ?? event?.userMessage ??
    ctx?.lastUserMessage ?? event?.message?.text ?? ""
  );
}

// Cheap stable fingerprint of the message, ONLY to detect "is this a new turn?".
// No semantic parsing — language-irrelevant by construction.
function fp(s: string): string {
  let h = 0;
  for (let i = 0; i < s.length; i++) { h = (h * 31 + s.charCodeAt(i)) | 0; }
  return String(h);
}

// ── register ─────────────────────────────────────────────────────────────────
export default definePluginEntry({
  name: "dinomem-recall-gate",
  description: "Mid-session recall enforcement (language-agnostic): blocks the first COLD " +
    "fs/exec reach on a turn until a recall tool has run. No message parsing.",
  register(api: any) {
    const c = cfg(api?.pluginConfig?.config);
    const recallList = c.recallTools.join("/");

    api.on("before_tool_call", async (event: any, ctx: any) => {
      try {
        const sessionKey: string = String(ctx?.sessionKey ?? event?.sessionKey ?? "");
        if (c.agentFilter && !sessionKey.includes(c.agentFilter)) return undefined;

        const toolName: string = String(event?.toolName ?? event?.tool ?? "");
        if (!toolName) return undefined;

        // Turn-boundary detection ONLY (no semantic parse of the message).
        const turnId = fp(userMsgOf(event, ctx) || sessionKey);

        let st = _state.get(sessionKey);
        if (!st || st.turnId !== turnId) {
          const turnIndex = (st?.turnIndex ?? 0) + 1;
          st = {
            turnId,
            turnIndex,
            recallDone: false,
            firedTurn: st?.firedTurn ?? -Infinity,
            writeFiredTurn: st?.writeFiredTurn ?? -Infinity,
            routeFiredTurn: st?.routeFiredTurn ?? -Infinity,
            routeDecisionDone: false,
            surfaceFiredTurn: st?.surfaceFiredTurn ?? -Infinity,
          };
          _state.set(sessionKey, st);
        }

        // A recall tool ran this turn -> latch cleared, gate silent until next turn.
        if (c.recallTools.includes(toolName)) {
          st.recallDone = true;
          return undefined;
        }

        // ── TIER D latch: did a routing decision run this turn? ──
        // route.py is a CLI (no dedicated tool name), so the signal is an exec
        // whose command mentions it. Latch BEFORE the write check below so the
        // normal flow -- classify, then write -- is never nudged.
        if (c.surfaceNudge && !st.routeDecisionDone) {
          const a: any = event?.toolArgs ?? event?.args ?? event?.input ??
                         event?.params ?? event?.arguments;
          const cmd = String(a?.command ?? a?.cmd ?? a?.script ?? "");
          if (cmd.includes(c.routeMarker)) st.routeDecisionDone = true;
        }

        // ── TIER D: durable-surface nudge on a memory write (SOFT, once/turn) ──
        // Deliberately NOT folded into Tier B: Tier B excludes memory/ for
        // loop-safety (a gate that fires on its own notes self-perpetuates), and
        // that exclusion must stay. This is a separate, weaker question asked only
        // of _pin_/_note_ writes.
        if (c.surfaceNudge && c.writeTools.includes(toolName) && !st.routeDecisionDone) {
          const paths = extractTargetPaths(event);
          const isSurfaceWrite = paths.some((p) =>
            c.surfacePaths.some((s) => String(p || "").includes(s)));
          const firedThisTurn = st.surfaceFiredTurn === st.turnIndex;
          if (isSurfaceWrite && !firedThisTurn && c.enforce) {
            st.surfaceFiredTurn = st.turnIndex;
            return {
              block: true,
              blockReason:
                `dinomem-recall-gate (surface): you're writing durable memory ` +
                `(${paths[0] ?? "memory file"}) without a routing decision this turn. One ` +
                `question first -- does this RECORD something (fact/decision/outcome/todo) ` +
                `or GOVERN future turns (rule/SOP/routing/trigger/persona)? RECORD -> memory ` +
                `is correct, just RE-ISSUE and it goes through (this won't fire again this ` +
                `turn). GOVERN -> run 'tools/route.py classify' and place it on the surface ` +
                `it names (cron|hook|skill|root) -- a rule in a _pin_ is retrieval-gated, so ` +
                `it never loads and the behaviour silently never happens. BOTH -> split them.`,
            };
          }
        }

        // ── TIER C: fuzzy-recall route enforcement (NEURON-only, OFF by default) ──
        // Route the raw gatedTool (memory_search) reflex to the fuzzy door
        // (hybrid_recall). EXEMPT: once routeTool ran this turn, gatedTool is
        // allowed -- hybrid_recall itself calls memory_search for --external-hits
        // folding, so blocking it post-door would self-stall the door. recallDone
        // is set by the recallTools latch above (hybrid_recall is in recallTools),
        // giving us a clean "door already opened this turn" signal. Soft-nudge once
        // (routeFiredTurn), hard-block on repeat within the turn.
        if (c.routeEnforce && toolName === c.gatedTool) {
          if (st.recallDone) return undefined;              // door opened this turn -> exempt
          // Capability gate: routeTool absent in THIS workspace -> never block.
          if (!hasRouteTool(c, String(event?.agentId ?? ctx?.agentId ?? ""), ctx)) return undefined;
          if (!c.enforce) return undefined;                 // dry-run
          const firedThisTurn = st.routeFiredTurn === st.turnIndex;
          st.routeFiredTurn = st.turnIndex;
          if (!firedThisTurn) {
            // FIRST reach this turn = soft nudge (let it through once).
            return {
              block: true,
              blockReason:
                `dinomem-recall-gate (route): you reached '${c.gatedTool}' directly. The ` +
                `dinomem-neuron "one fuzzy door" is '${c.routeTool}' -- it fires the right legs, ` +
                `fuses + reranks with provenance (and folds '${c.gatedTool}' via --external-hits ` +
                `internally). Prefer '${c.routeTool}' for semantic recall. If you specifically ` +
                `need raw '${c.gatedTool}' this turn, just re-issue -- this soft nudge won't ` +
                `repeat, but a THIRD blind reach will hard-block.`,
            };
          }
          // SECOND+ reach this turn without opening the door = hard block.
          return {
            block: true,
            blockReason:
              `dinomem-recall-gate (route, HARD): repeated raw '${c.gatedTool}' this turn without ` +
              `going through '${c.routeTool}'. Run '${c.routeTool} "<natural-language query>"' ` +
              `instead -- it folds '${c.gatedTool}' hits for you. (If '${c.routeTool}' is ` +
              `genuinely broken, that's a separate bug to report, not a reason to bypass the door.)`,
          };
        }

        // ── TIER B: hot-zone WRITE re-arm ──
        // Building on a mechanism script/config is a distinct danger from reading.
        // A general recall latched EARLIER in the turn does NOT satisfy it (that
        // was the exact hole: recalled about an investigation, then built a retry
        // blind). Requires a FRESH recall THIS turn. Loop-safe via hotZoneExclude.
        if (c.writeTools.includes(toolName)) {
          const paths = extractTargetPaths(event);
          if (!pathIsHotZone(paths, c)) return undefined;   // non-mechanism write: never gated
          if (st.recallDone) return undefined;              // fresh recall this turn satisfies it
          if (st.turnIndex - st.writeFiredTurn < c.cooldownTurns) return undefined;
          const alreadyWroteFire = st.writeFiredTurn === st.turnIndex;
          st.writeFiredTurn = st.turnIndex;
          if (alreadyWroteFire) return undefined;
          if (!c.enforce) return undefined;                 // dry-run
          return {
            block: true,
            blockReason:
              `dinomem-recall-gate: you're about to '${toolName}' a MECHANISM file ` +
              `(${paths[0] ?? "hot-zone path"}) without a fresh recall this turn. Building on a ` +
              `system with history? recall its prior decisions/failures FIRST (${recallList}) ` +
              `-- an earlier recall about something else does NOT cover this build. Then retry; ` +
              `the gate won't fire again this turn.`,
          };
        }

        // Only fs/exec-shaped tools are gated.
        if (!c.fsTools.includes(toolName)) return undefined;

        // Recall already happened this turn -> no gate. (THE core rule: cold reach
        // = no recall yet this turn. Language-independent by construction.)
        if (st.recallDone) return undefined;

        // Cooldown: don't nag every turn if the model keeps skipping recall.
        if (st.turnIndex - st.firedTurn < c.cooldownTurns) return undefined;

        // Fire ONCE per turn. Do NOT hard-loop-block: if the model retries the same
        // fs tool this turn without recalling, let it through rather than stall the
        // turn. The nudge fired; persistent-ignore is now a model-quality problem,
        // not a missing-injection one.
        const alreadyFiredThisTurn = st.firedTurn === st.turnIndex;
        st.firedTurn = st.turnIndex;
        if (alreadyFiredThisTurn) return undefined;

        if (!c.enforce) return undefined; // dry-run: would have blocked, didn't

        return {
          block: true,
          blockReason:
            `dinomem-recall-gate: you're reaching for '${toolName}' before running any recall ` +
            `tool this turn. If this touches prior work/context, recall FIRST (${recallList}), ` +
            `then retry. If it's genuinely fresh work with nothing to recall, just re-issue — ` +
            `the gate won't fire again this turn.`,
        };
      } catch (err) {
        // Fail open, always. A recall nudge must never brick the tool loop.
        return undefined;
      }
    });
  },
});
