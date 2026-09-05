/**
 * selftest.mjs — dinomem-retrieval-log pure-logic proof (no gateway needed).
 *
 * We can't import index.ts (TS + plugin-sdk) under plain node, so this re-implements
 * the PURE helpers (cfg / resolveWorkspace ladder / tool-filter / extractors) with
 * the SAME logic and asserts their contracts. If you change index.ts logic, mirror
 * it here. The real end-to-end (does a native memory_search land a source:native
 * line) is proven live against the gateway, not here.
 *
 * Run: node selftest.mjs   (exit 0 = all pass)
 */
import * as path from "node:path";
import * as fs from "node:fs";
import os from "node:os";
import { execFileSync } from "node:child_process";

let pass = 0, fail = 0;
const ok = (name, cond) => { if (cond) { pass++; } else { fail++; console.error("FAIL:", name); } };

// ---- mirror of cfg() ----
function cfg(raw, env = {}) {
  const c = raw ?? {};
  const envOff = String(env.DINOMEM_LOG_NATIVE_RECALL ?? "") === "0";
  return {
    enabled: envOff ? false : (c.enabled ?? true),
    agentFilter: typeof c.agentFilter === "string" ? c.agentFilter : "",
    nativeTools: Array.isArray(c.nativeTools) && c.nativeTools.length
      ? c.nativeTools.map((s) => String(s)) : ["memory_search", "memory_get"],
    workspace: typeof c.workspace === "string" ? c.workspace : "",
    python: typeof c.python === "string" && c.python ? c.python : "python3",
  };
}
// ---- mirror of extractors ----
function extractQuery(event) {
  const p = event?.params ?? event?.toolArgs ?? event?.args ?? event?.input ?? {};
  const q = p.query ?? p.q ?? p.search ?? p.text ?? p.path ?? "";
  return typeof q === "string" ? q : "";
}
function extractK(event) {
  const p = event?.params ?? event?.toolArgs ?? event?.args ?? event?.input ?? {};
  const k = p.k ?? p.limit ?? p.maxResults ?? p.max_results ?? p.lines;
  const n = Number(k);
  return Number.isFinite(n) ? n : null;
}
function extractNResults(event) {
  const r = event?.result ?? event?.toolResult ?? event?.output ?? event?.response ?? null;
  if (r == null) return null;
  const arr = (Array.isArray(r) && r) || (Array.isArray(r?.results) && r.results)
    || (Array.isArray(r?.matches) && r.matches) || (Array.isArray(r?.hits) && r.hits)
    || (Array.isArray(r?.items) && r.items) || null;
  return arr ? arr.length : null;
}
function hasLogger(dir) { try { return fs.existsSync(path.join(dir, "procedures", "_retrieval_log.py")); } catch { return false; } }
function walkUpForLogger(start) {
  let d = start;
  for (let i = 0; i < 8 && d && d !== path.dirname(d); i++) { if (hasLogger(d)) return d; d = path.dirname(d); }
  return null;
}

// ===== config contract =====
ok("default enabled true", cfg({}).enabled === true);
ok("env DINOMEM_LOG_NATIVE_RECALL=0 disables", cfg({}, { DINOMEM_LOG_NATIVE_RECALL: "0" }).enabled === false);
ok("explicit enabled false", cfg({ enabled: false }).enabled === false);
ok("default nativeTools = 2 base memory tools", JSON.stringify(cfg({}).nativeTools) === JSON.stringify(["memory_search", "memory_get"]));
ok("custom nativeTools honored", cfg({ nativeTools: ["memory_search"] }).nativeTools.length === 1);
ok("default agentFilter empty (all agents)", cfg({}).agentFilter === "");
ok("default python python3", cfg({}).python === "python3");

// ===== tool filter =====
const nativeSet = new Set(cfg({}).nativeTools);
ok("filter: memory_search matches", nativeSet.has("memory_search"));
ok("filter: memory_get matches", nativeSet.has("memory_get"));
ok("filter: exec does NOT match", !nativeSet.has("exec"));
ok("filter: hybrid_recall does NOT match (that's a python-tool, self-logs)", !nativeSet.has("hybrid_recall"));

// ===== extractors =====
ok("query from params.query", extractQuery({ params: { query: "btc dca" } }) === "btc dca");
ok("query from args.q fallback", extractQuery({ args: { q: "foo" } }) === "foo");
ok("query from path (memory_get)", extractQuery({ params: { path: "memory/2026-08-01.md" } }) === "memory/2026-08-01.md");
ok("query empty when absent", extractQuery({ params: {} }) === "");
ok("k from params.k", extractK({ params: { k: 5 } }) === 5);
ok("k from limit fallback", extractK({ params: { limit: 8 } }) === 8);
ok("k null when absent", extractK({ params: {} }) === null);
ok("nResults from result array", extractNResults({ result: [1, 2, 3] }) === 3);
ok("nResults from result.results", extractNResults({ result: { results: [1, 2] } }) === 2);
ok("nResults from result.hits", extractNResults({ result: { hits: [1] } }) === 1);
ok("nResults null when unknowable", extractNResults({ result: { note: "no array" } }) === null);
ok("nResults null when no result field", extractNResults({}) === null);

// ===== WS resolution ladder (real fs) =====
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "dm-rl-ws-"));
const wsRoot = path.join(tmp, "workspace-x");
fs.mkdirSync(path.join(wsRoot, "procedures"), { recursive: true });
fs.writeFileSync(path.join(wsRoot, "procedures", "_retrieval_log.py"), "# stub\n");
const deep = path.join(wsRoot, "plugins", "dinomem-retrieval-log");
fs.mkdirSync(deep, { recursive: true });
ok("hasLogger true at ws root", hasLogger(wsRoot));
ok("hasLogger false at deep leaf", !hasLogger(deep));
ok("walkUp from deep finds ws root", walkUpForLogger(deep) === wsRoot);
ok("walkUp returns null when no logger anywhere", walkUpForLogger(tmp) === null);

// ===== end-to-end: the ladder target's --record actually writes (uses REAL logger) =====
// Point at the real repo logger (two dirs up: plugins/<name>/ -> repo root).
const repoRoot = walkUpForLogger(path.dirname(new URL(import.meta.url).pathname));
if (repoRoot && fs.existsSync(path.join(repoRoot, "procedures", "_retrieval_log.py"))) {
  const ews = fs.mkdtempSync(path.join(os.tmpdir(), "dm-rl-e2e-"));
  try {
    execFileSync("python3", [
      path.join(repoRoot, "procedures", "_retrieval_log.py"),
      "--record", "--workspace", ews, "--tool", "memory_search",
      "--source", "native", "--query", "selftest e2e", "--k", "5", "--n-results", "2",
    ], { stdio: "ignore" });
    const day = new Date().toISOString().slice(0, 10);
    const line = fs.readFileSync(path.join(ews, "kb", "retrieval_log", `${day}.jsonl`), "utf-8").trim();
    const rec = JSON.parse(line.split("\n").pop());
    ok("e2e: record wrote source=native", rec.source === "native");
    ok("e2e: record tool=memory_search", rec.tool === "memory_search");
    ok("e2e: record n_results=2", rec.n_results === 2);
  } catch (e) {
    fail++; console.error("FAIL: e2e record path threw:", String(e).slice(0, 200));
  }
} else {
  console.error("SKIP e2e: repo logger not found from selftest location");
}

console.log(`\n${pass} passed / ${fail} failed`);
process.exit(fail ? 1 : 0);
