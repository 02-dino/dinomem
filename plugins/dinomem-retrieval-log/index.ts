/**
 * dinomem-retrieval-log — capture NATIVE OpenClaw memory tools into the dinomem
 * retrieval_log (the analyst-bias fix).
 *
 * PROBLEM (dino, 2026-09-05): kb/retrieval_log/*.jsonl only captured the explicit
 * dinomem PYTHON retrieval tools (hybrid_recall / session_search / docs_search /
 * graph_search / data_query) because THEY call procedures/_retrieval_log.py from
 * inside their own code. But most agents recall via the NATIVE OpenClaw gateway
 * tools `memory_search` / `memory_get`, which are NOT dinomem python and never
 * touched the logger. Result: recall metrics read as ~0 for every non-analyst
 * agent, making the "dinomem effectiveness per agent" report unfair. This plugin
 * closes that gap.
 *
 * WHY A TYPED PLUGIN AND NOT AN INTERNAL HOOK: the internal dinomem hook system
 * (command:* / session:* / message:* / gateway:* / agent:bootstrap) has NO event
 * that fires on a tool call. Only the typed plugin surface (api.on) sees tool
 * calls. `after_tool_call` is observation-only ("observe tool results, errors,
 * duration") — exactly right: we watch, we never block. (Modeled on the sibling
 * dinomem-recall-gate plugin, which uses before_tool_call on the same surface.)
 *
 * DESIGN (reuse, no fork): this plugin writes NOTHING itself. On a matching native
 * tool it shells out FIRE-AND-FORGET to `procedures/_retrieval_log.py --record`
 * (the SAME writer the python tools use) with source="native". One JSONL schema,
 * one writer, in Python. TS never re-implements the record format.
 *
 * GENERALIZED (public-install acceptance bar — the whole point): agent-agnostic.
 * No hardcoded agent id / workspace / chat id. Workspace is resolved dynamically
 * from the hook runtime context, then env, then a plugin-dir walk-up. Fail-OPEN
 * in every branch: a logging miss must NEVER slow or break the tool loop.
 */
// NOTE: the scoped "@openclaw/plugin-sdk" import silently failed to load in this
// runtime for the sibling gate plugin (handler never fired). Use the same working
// path that plugin settled on.
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { spawn } from "node:child_process";
import * as path from "node:path";
import * as fs from "node:fs";

type Cfg = {
  enabled: boolean;
  agentFilter: string;
  nativeTools: string[];
  workspace: string;
  python: string;
};

function cfg(raw: any): Cfg {
  const c = raw ?? {};
  const envOff = String(process.env.DINOMEM_LOG_NATIVE_RECALL ?? "") === "0";
  return {
    enabled: envOff ? false : (c.enabled ?? true),
    agentFilter: typeof c.agentFilter === "string" ? c.agentFilter : "",
    nativeTools:
      Array.isArray(c.nativeTools) && c.nativeTools.length
        ? c.nativeTools.map((s: unknown) => String(s))
        : ["memory_search", "memory_get"],
    workspace: typeof c.workspace === "string" ? c.workspace : "",
    python: typeof c.python === "string" && c.python ? c.python : "python3",
  };
}

// Resolve the dinomem workspace root WITHOUT hardcoding it. A valid dinomem
// workspace has a procedures/_retrieval_log.py under it. Ladder (first hit wins):
//   1. explicit config.workspace
//   2. DINOMEM_WORKSPACE / OPENCLAW_WORKSPACE env
//   3. hook ctx cwd, if it (or an ancestor) contains procedures/_retrieval_log.py
//   4. walk up from this plugin file (…/<ws>/plugins/dinomem-retrieval-log/)
// Cached per resolved path so we don't stat the tree on every tool call.
const _wsCache = new Map<string, string | null>();
function hasLogger(dir: string): boolean {
  try {
    return fs.existsSync(path.join(dir, "procedures", "_retrieval_log.py"));
  } catch {
    return false;
  }
}
function walkUpForLogger(start: string): string | null {
  let d = start;
  for (let i = 0; i < 8 && d && d !== path.dirname(d); i++) {
    if (hasLogger(d)) return d;
    d = path.dirname(d);
  }
  return null;
}
// PRIMARY resolver (proven live 2026-09-05): the after_tool_call ctx/event carries
// `agentId` (e.g. "analyst") but NO workspace path (ctx.cwd is null, gateway cwd is
// /root). OpenClaw's per-agent workspace convention is <OPENCLAW_DIR>/workspace-<id>,
// so we derive it from agentId + candidate roots and confirm _retrieval_log.py exists.
// This is what makes the plugin GENERALIZED: any agent id on any box resolves to its
// own workspace with zero hardcoding. Env/config overrides still win for odd layouts.
function openclawRoots(): string[] {
  const roots: string[] = [];
  const push = (d?: string) => { if (d && !roots.includes(d)) roots.push(d); };
  push(process.env.OPENCLAW_DIR);
  push(process.env.OPENCLAW_HOME);
  const home = process.env.HOME || "/root";
  push(path.join(home, ".openclaw"));
  // per-instance dirs (~/.openclaw-<id>) exist on some multi-instance boxes.
  push("/root/.openclaw");
  return roots;
}
function wsFromAgentId(agentId: string): string | null {
  if (!agentId) return null;
  for (const root of openclawRoots()) {
    // both the shared-dir layout (<root>/workspace-<id>) and the per-instance
    // layout (<root>-<id>/workspace-<id> or <root>-<id>) are checked.
    const candidates = [
      path.join(root, `workspace-${agentId}`),
      path.join(`${root}-${agentId}`, `workspace-${agentId}`),
      `${root}-${agentId}`,
    ];
    for (const cnd of candidates) if (hasLogger(cnd)) return cnd;
  }
  return null;
}
function resolveWorkspace(c: Cfg, event: any, ctx: any): string | null {
  const agentId = String(event?.agentId ?? ctx?.agentId ?? "");
  const cacheKey = `${c.workspace}|${agentId}`;
  if (_wsCache.has(cacheKey)) return _wsCache.get(cacheKey)!;
  let ws: string | null = null;
  // 1) explicit config override
  if (c.workspace && hasLogger(c.workspace)) ws = c.workspace;
  // 2) PRIMARY: derive from agentId (the generalized path)
  if (!ws) ws = wsFromAgentId(agentId);
  // 3) env fallback (single-workspace boxes)
  if (!ws) {
    for (const e of [process.env.DINOMEM_WORKSPACE, process.env.OPENCLAW_WORKSPACE]) {
      if (e && hasLogger(e)) { ws = e; break; }
    }
  }
  // 4) ctx cwd walk-up, if the runtime ever populates it
  if (!ws) {
    const ctxCwd = ctx?.cwd ?? ctx?.workspace ?? ctx?.workspaceDir;
    if (typeof ctxCwd === "string" && ctxCwd) ws = walkUpForLogger(ctxCwd);
  }
  _wsCache.set(cacheKey, ws);
  return ws;
}

// Pull a best-effort query string + result count out of the native tool payload.
// Shapes differ per tool/runtime, so probe several field names; null what's absent
// (the logger clips/normalizes). We NEVER fabricate scores — native tools may not
// expose them, and a null is honest.
function extractQuery(event: any): string {
  const p: any = event?.params ?? event?.toolArgs ?? event?.args ?? event?.input ?? {};
  const q = p.query ?? p.q ?? p.search ?? p.text ?? p.path ?? "";
  return typeof q === "string" ? q : "";
}
function extractK(event: any): number | null {
  const p: any = event?.params ?? event?.toolArgs ?? event?.args ?? event?.input ?? {};
  const k = p.k ?? p.limit ?? p.maxResults ?? p.max_results ?? p.lines;
  const n = Number(k);
  return Number.isFinite(n) ? n : null;
}
function countArrayish(r: any): number | null {
  if (r == null) return null;
  const arr =
    (Array.isArray(r) && r) ||
    (Array.isArray(r?.results) && r.results) ||
    (Array.isArray(r?.matches) && r.matches) ||
    (Array.isArray(r?.hits) && r.hits) ||
    (Array.isArray(r?.items) && r.items) ||
    null;
  return arr ? arr.length : null;
}
function extractNResults(event: any): number | null {
  // Proven live shape (2026-09-05): event.result = { content:[{type,text}], details:{...} }
  // where `text` is the JSON payload string and `details` is the parsed object.
  // memory_search returns {results:[...]}, so count details.results / parsed.results.
  const r: any =
    event?.result ?? event?.toolResult ?? event?.output ?? event?.response ?? null;
  if (r == null) return null;
  // 1) direct array-ish on the result
  let n = countArrayish(r);
  if (n != null) return n;
  // 2) parsed `details` object (native tool result envelope)
  n = countArrayish(r?.details);
  if (n != null) return n;
  // 3) content[].text is a JSON string — parse and count
  try {
    const txt = Array.isArray(r?.content)
      ? r.content.map((x: any) => (typeof x?.text === "string" ? x.text : "")).join("")
      : (typeof r?.text === "string" ? r.text : "");
    if (txt) {
      const parsed = JSON.parse(txt);
      n = countArrayish(parsed);
      if (n != null) return n;
    }
  } catch { /* not JSON / no array — null is honest */ }
  return null;
}
function extractLatencyMs(event: any): number | null {
  const d = event?.durationMs ?? event?.latencyMs ?? event?.elapsedMs;
  const n = Number(d);
  return Number.isFinite(n) ? Math.round(n) : null;
}

export default definePluginEntry({
  id: "dinomem-retrieval-log",
  register(api: any) {
    // Match dinotrust/recall-gate: config can arrive as pluginConfig.config OR
    // pluginConfig OR config depending on gateway version — 3-way unwrap.
    const c = cfg(api?.pluginConfig?.config ?? api?.pluginConfig ?? api?.config);
    if (!c.enabled) return; // hard off: register nothing.
    const nativeSet = new Set(c.nativeTools);

    // We log AFTER the tool runs so we can capture result count + latency without
    // adding any latency to the tool loop (observation-only, fire-and-forget).
    api.on("after_tool_call", async (event: any, ctx: any) => {
      try {
        const toolName = String(event?.toolName ?? event?.tool ?? "");
        if (!nativeSet.has(toolName)) return; // only our native memory tools

        const sessionKey = String(ctx?.sessionKey ?? event?.sessionKey ?? "");
        if (c.agentFilter && !sessionKey.includes(c.agentFilter)) return;

        // Resolve WS from agentId (proven ctx field) — the generalized path.
        const ws = resolveWorkspace(c, event, ctx);
        if (!ws) return; // can't find the logger -> no-op, fail open

        const query = extractQuery(event);
        const k = extractK(event);
        const n = extractNResults(event);
        const latencyMs = extractLatencyMs(event);
        const hadError = event?.error != null;

        const argv = [
          path.join(ws, "procedures", "_retrieval_log.py"),
          "--record",
          "--workspace", ws,
          "--tool", toolName,
          "--source", "native",
        ];
        if (query) argv.push("--query", query);
        if (k != null) argv.push("--k", String(k));
        if (n != null) argv.push("--n-results", String(n));
        // Logger has no --latency flag; carry latency+error via --extra-json.
        const extra: Record<string, unknown> = {};
        if (latencyMs != null) extra.latency_ms = latencyMs;
        if (hadError) extra.error = true;
        if (Object.keys(extra).length) argv.push("--extra-json", JSON.stringify(extra));

        // Fire-and-forget: after_tool_call is observation-only, so we MUST NOT
        // add latency to the tool loop. Detach, ignore io, unref, swallow errors.
        const child = spawn(c.python, argv, {
          stdio: "ignore",
          detached: true,
          env: { ...process.env, DINOMEM_WORKSPACE: ws },
        });
        child.on("error", () => { /* fail open: logging must never throw */ });
        child.unref();
      } catch {
        // Fail open, always.
        return;
      }
    });
  },
});
