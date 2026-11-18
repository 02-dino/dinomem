// Pure detection logic for cross-instance-exec-warn (no SDK import -> testable
// with plain `node detect.test.mjs`; index.ts needs the SDK, which only resolves
// inside the gateway loader).
//
// The bug class: an exec inherits the CALLER gateway's OPENCLAW_CONFIG_PATH /
// OPENCLAW_STATE_DIR / OPENCLAW_GATEWAY_PORT, so a script that belongs to another
// instance resolves the caller's config and talks to the caller's gateway.
//
// Ownership is MEASURED, not guessed from names: every <home>/.openclaw[-X]/
// openclaw.json is read and each agents.list[].workspace is mapped to its
// instance. Name-guessing was wrong on the box this was built on: workspace-
// summarizer belongs to analyst and workspace-wasales to sales.
//
// Only RUNNING a foreign script is flagged. Reading one (cat/grep/ls <path>) is
// harmless, so only the COMMAND position of each shell segment is inspected.
import { readFileSync, readdirSync, existsSync } from "node:fs";
import { join, resolve, dirname, basename } from "node:path";

const RUNNERS = new Set(["python", "python3", "bash", "sh", "zsh", "node", "npx", "tsx", "deno", "bun"]);
const PREFIXES = new Set(["env", "nohup", "sudo", "exec", "time", "setsid"]);
// The caller already chose the target instance -> nothing to warn about.
const EXPLICIT = /run-as-instance\.sh|OPENCLAW_CONFIG_PATH=|OPENCLAW_STATE_DIR=/;

const norm = (p) => String(p || "").replace(/\/+$/, "");

/** instance name of a state dir: .openclaw -> "default", .openclaw-cs -> "cs" */
export function instanceOfStateDir(dir) {
  const b = basename(norm(dir));
  if (b === ".openclaw") return "default";
  const m = b.match(/^\.openclaw-(.+)$/);
  return m ? m[1] : null;
}

/** Own instance + home, from the gateway's own env. */
export function selfFromEnv(env) {
  const state = norm(env.OPENCLAW_STATE_DIR) ||
    (env.OPENCLAW_CONFIG_PATH ? dirname(env.OPENCLAW_CONFIG_PATH) : "") ||
    join(env.HOME || "/root", ".openclaw");
  return { own: instanceOfStateDir(state) || "default", home: dirname(state) };
}

/** -> [[absPrefix, instance], ...] longest prefix first. Unreadable configs are skipped. */
export function buildOwnerMap(home) {
  const out = [];
  let names = [];
  try { names = readdirSync(home); } catch { return out; }
  for (const n of names) {
    const inst = instanceOfStateDir(n);
    if (!inst) continue;
    const state = join(home, n);
    out.push([state, inst]);
    const cfgPath = join(state, "openclaw.json");
    if (!existsSync(cfgPath)) continue;
    try {
      const cfg = JSON.parse(readFileSync(cfgPath, "utf8"));
      for (const a of cfg?.agents?.list ?? []) {
        if (a && typeof a.workspace === "string" && a.workspace) out.push([norm(a.workspace), inst]);
      }
    } catch { /* JSON5 or broken config: that instance just stays unmapped */ }
  }
  // longest first, so <home>/.openclaw/workspace-cs beats <home>/.openclaw
  return out.sort((x, y) => y[0].length - x[0].length);
}

function ownerOf(path, map) {
  for (const [prefix, inst] of map) {
    if (path === prefix || path.startsWith(prefix + "/")) return { prefix, inst };
  }
  return null;
}

/** Split on ; & | ( ) newline OUTSIDE quotes -- a separator inside a quoted
 *  `python3 -c "open('x')"` arg must not start a fake command segment. */
function segments(command) {
  const out = [];
  let cur = "", q = "";
  for (const ch of command) {
    if (q) { if (ch === q) q = ""; cur += ch; continue; }
    if (ch === "'" || ch === '"') { q = ch; cur += ch; continue; }
    if (";&|()\n".includes(ch)) { out.push(cur); cur = ""; continue; }
    cur += ch;
  }
  out.push(cur);
  return out;
}

/** The script each shell segment would run. */
function scriptsRun(command) {
  const out = [];
  for (const seg of segments(command)) {
    const t = seg.trim().split(/\s+/).filter(Boolean);
    let i = 0;
    while (i < t.length) {
      if (/^[A-Za-z_][A-Za-z0-9_]*=/.test(t[i]) || PREFIXES.has(t[i]) || /^-/.test(t[i])) { i++; continue; }
      if (t[i] === "timeout" && i + 1 < t.length) { i += 2; continue; }
      break;
    }
    if (i >= t.length) continue;
    const head = t[i].replace(/^["']|["']$/g, "");
    if (RUNNERS.has(basename(head).replace(/[0-9.]+$/, "")) || /python[0-9.]*$/.test(head)) {
      let j = i + 1;
      while (j < t.length && /^-/.test(t[j])) { if (t[j] === "-m" || t[j] === "-c") { j = t.length; break; } j++; }
      if (j < t.length) out.push(t[j].replace(/^["']|["']$/g, ""));
    } else {
      out.push(head);
    }
  }
  return out;
}

/**
 * -> foreign instance name the command RUNS a script of, or null.
 * The caller's own instance never warns; neither does a path no instance owns
 * (shared dirs like <home>/.openclaw/extensions belong to the default state dir,
 * but only when the caller IS default would that be "own" -- so shared infra is
 * skipped explicitly: only paths inside a mapped WORKSPACE or a NON-default state
 * dir count).
 */
export function foreignInstance(command, ctx) {
  if (typeof command !== "string" || !command || EXPLICIT.test(command)) return null;
  const { own, map, cwd } = ctx;
  for (const s of scriptsRun(command)) {
    if (!s.includes("/")) continue;              // bare binary from PATH
    const abs = s.startsWith("/") ? norm(s) : (cwd ? resolve(cwd, s) : null);
    if (!abs) continue;
    const hit = ownerOf(abs, map);
    if (!hit || hit.inst === own) continue;
    // the default state dir doubles as shared infra (extensions/, docs, other
    // workspaces' parent); only its mapped workspaces are really "default's".
    if (hit.inst === "default" && instanceOfStateDir(hit.prefix) === "default") continue;
    return hit.inst;
  }
  return null;
}

export function warnText(inst, own) {
  return (
    `cross-instance-exec-warn: this exec runs a script owned by instance '${inst}', but ` +
    `your shell carries ${own}'s OPENCLAW_CONFIG_PATH/STATE_DIR/GATEWAY_PORT, so it will ` +
    `read ${own}'s config and talk to ${own}'s gateway. Prefer:\n` +
    `  bash scripts/run-as-instance.sh ${inst} <cmd>\n` +
    `This is a WARNING only: re-issue the exact same command to run it as-is.`
  );
}
