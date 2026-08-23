import { existsSync } from "node:fs";
import { join, isAbsolute } from "node:path";
import { spawnSync } from "node:child_process";

// context-inject — on message:received, when the inbound message EXPLICITLY names
// a file path or a backtick-wrapped code symbol, front-load that context ONCE:
//   - `git diff` of the named file (base; git only)
//   - `code_query explain <symbol>` (NEURON; fail-open — skipped cleanly if the
//     tool/graph is absent, so a base-only install still works)
// into the model's turn context. This mimics an IDE auto-injecting the open file
// + symbol graph, but ON DEMAND: a ZERO-LLM regex gate means a non-code message
// ("hi", a market question) injects NOTHING → zero added tokens. On a code turn
// it front-loads a read the model would do anyway, so it's ~cost-neutral.
//
// react-only: never blocks/rewrites. Never breaks the pipeline (try/catch).
// TIGHT TRIGGER (the whole cost story): fire ONLY on an explicit path (has a
// slash or a known code extension) or a backtick-wrapped symbol. Plain prose
// words never match. Loose matching here = false-positive injection = wasted
// tokens, so the gate is deliberately strict.

type MaybeRecord = Record<string, unknown> | undefined | null;

function asString(v: unknown): string | undefined {
  return typeof v === "string" && v.length > 0 ? v : undefined;
}

function resolveWorkspaceDir(context: MaybeRecord): string | undefined {
  const ctx = (context ?? {}) as Record<string, unknown>;
  const direct = asString(ctx.workspaceDir);
  if (direct) return direct;
  const cfg = ctx.cfg as MaybeRecord;
  if (cfg && typeof cfg === "object") {
    const ws = (cfg as Record<string, unknown>).workspace as MaybeRecord;
    if (ws && typeof ws === "object") {
      const dir = asString((ws as Record<string, unknown>).dir);
      if (dir) return dir;
    }
  }
  return asString(process.env.OPENCLAW_WORKSPACE) ?? asString(process.env.DINOMEM_WORKSPACE);
}

// Code file extensions we consider a "real path" without needing a slash.
const CODE_EXT =
  /\.(py|sh|bash|js|mjs|cjs|ts|tsx|json|go|rs|java|rb|php|c|cpp|h|hpp|css|html|sql|ya?ml|md)$/i;

// Extract at most N distinct file-path candidates from the message.
// A candidate is a whitespace-free token that EITHER contains a slash (path-ish)
// OR ends in a known code extension. Backtick/quote wrappers are stripped.
function extractPaths(msg: string, max = 3): string[] {
  const out: string[] = [];
  const seen = new Set<string>();
  // tokens inside backticks first (highest intent), then bare tokens
  const toks = [
    ...(msg.match(/`([^`]+)`/g) ?? []).map((s) => s.replace(/`/g, "")),
    ...msg.split(/\s+/),
  ];
  for (let t of toks) {
    t = t.replace(/^[('"[]+|[)'"\].,;:]+$/g, "").trim();
    if (!t || t.length > 200) continue;
    const looksPath = t.includes("/") || CODE_EXT.test(t);
    if (!looksPath) continue;
    if (seen.has(t)) continue;
    seen.add(t);
    out.push(t);
    if (out.length >= max) break;
  }
  return out;
}

// Extract backtick-wrapped SYMBOLS (identifiers) that are NOT paths — candidates
// for code_query explain. e.g. `resolveWorkspaceDir`, `guard_by_hash`.
function extractSymbols(msg: string, max = 2): string[] {
  const out: string[] = [];
  const seen = new Set<string>();
  for (const m of msg.match(/`([A-Za-z_][A-Za-z0-9_.]{2,})`/g) ?? []) {
    const s = m.replace(/`/g, "");
    if (s.includes("/") || CODE_EXT.test(s)) continue; // that's a path, not a symbol
    if (seen.has(s)) continue;
    seen.add(s);
    out.push(s);
    if (out.length >= max) break;
  }
  return out;
}

function firstMessage(event: { messages?: string[] }): string | undefined {
  const arr = event.messages;
  if (!Array.isArray(arr) || arr.length === 0) return undefined;
  const s = arr.map((m) => (typeof m === "string" ? m : "")).join("\n").trim();
  return s.length > 0 ? s : undefined;
}

function run(cmd: string, args: string[], cwd: string): string | undefined {
  try {
    const r = spawnSync(cmd, args, { cwd, encoding: "utf8", timeout: 8000 });
    if (r.status === 0 && typeof r.stdout === "string" && r.stdout.trim().length > 0) {
      return r.stdout.trim();
    }
  } catch {
    /* fail-open */
  }
  return undefined;
}

function cap(s: string, max: number): string {
  return s.length > max ? s.slice(0, max) + "\n… (truncated)" : s;
}

const handler = async (event: {
  type?: string;
  action?: string;
  messages?: string[];
  context?: MaybeRecord;
}): Promise<void> => {
  try {
    // EVENT GUARD
    if (event?.type !== "message" || event?.action !== "received") return;

    const msg = firstMessage(event);
    if (!msg) return;

    // ── ZERO-LLM GATE ── (no model call; pure regex). No path & no symbol → bail.
    const paths = extractPaths(msg);
    const symbols = extractSymbols(msg);
    if (paths.length === 0 && symbols.length === 0) return; // non-code turn → zero cost

    const ws = resolveWorkspaceDir(event.context);
    if (!ws) return;

    const blocks: string[] = [];

    // ── DIFF LEG (base): git diff for each named file that exists in the ws ──
    for (const p of paths) {
      const abs = isAbsolute(p) ? p : join(ws, p);
      if (!existsSync(abs)) continue;
      const diff = run("git", ["-C", ws, "diff", "--", p], ws)
        ?? run("git", ["-C", ws, "diff", "--", abs], ws);
      if (diff) blocks.push("### git diff — " + p + "\n```diff\n" + cap(diff, 2000) + "\n```");
    }

    // ── SYMBOL LEG (neuron, FAIL-OPEN): code_query explain <symbol> ──
    // Only attempt if the tool exists; a base-only install silently skips it.
    const cq = join(ws, "tools", "code_query.py");
    if (existsSync(cq)) {
      for (const s of symbols) {
        const out = run("python3", [cq, "explain", s], ws);
        if (out) blocks.push("### code_query explain — " + s + "\n```\n" + cap(out, 1500) + "\n```");
      }
    }

    if (blocks.length === 0) return; // named things but nothing to show → no injection

    const header =
      "📎 Auto-injected context (dinomem context-inject) for files/symbols you named — " +
      "use it to skip a manual read:";
    const payload = header + "\n\n" + blocks.join("\n\n");

    // react-only side effect: push into the model's turn on this replyable surface.
    if (Array.isArray(event.messages)) event.messages.push(payload);
    console.log(
      `[context-inject] injected ${blocks.length} block(s) for ${ws}`,
    );
  } catch (err) {
    console.warn("[context-inject] handler error: " + String(err));
  }
};

export default handler;
