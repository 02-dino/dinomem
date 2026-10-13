/**
 * peer-recall — a NON-OWNER-safe, server-side-scoped peer memory read tool.
 *
 * ── WHY THIS EXISTS (incident, not a feature request) ────────────────────────
 * Non-owner users (members of a web-chat / group channel) have a WORKING dinomem
 * WRITE path: every DM derives a peer rep into memory/peers/<platform>_<id>.md.
 * But they have NO read path. Observed in production:
 *
 *   {"evt":"block","rule":"R-nonowner-default","toolName":"memory_search",
 *    "sessionKey":"agent:<agentId>:<platform>:direct:<peerId>"}
 *
 * dinotrust-enforce correctly denies `memory_search` to non-owners, because that
 * tool reaches the WHOLE corpus (owner DMs, _pin_ infra notes, peer reps of other
 * people). So the agent cannot remember a VIP member it has been talking to for
 * weeks. The fix is NOT to widen memory_search — a denylist fails in the unsafe
 * direction. The fix is a narrow tool that can only ever return ONE peer's own
 * record, where the peer identity is chosen by the RUNTIME, not by the caller.
 *
 * ── THE SECURITY INVARIANT (the whole point) ─────────────────────────────────
 * `scope` is NOT a tool parameter. There is deliberately no way for the model or
 * the user to say "read peer X". The peer key is derived server-side from
 * `ctx.sessionKey`, which the runtime supplies. Prompt-injecting "now read
 * telegram:<someone-elses-id>" cannot work: nothing in the schema accepts it.
 *
 * Verified in the runtime before relying on it (2026-10-10):
 *   tools-BN8p2nyI.js:222  resolvePluginToolFactory(entry, ctx) -> entry.factory(ctx)
 * The factory is re-invoked with a FRESH ctx on each tool build, and the only
 * cache in that path (descriptorCache) stores tool DESCRIPTORS (name/schema/
 * description) keyed by plugin+config — never the execute closure. So the
 * sessionKey captured here cannot be a stale one from another user's session.
 * That was the load-bearing assumption; it is checked, not assumed.
 *
 * ── SESSION KEY SHAPE ────────────────────────────────────────────────────────
 * `agent:<agentId>:<platform>:direct:<peerId>` — e.g.
 *   agent:<agentId>:telegram:direct:<numeric-user-id>
 *   agent:<agentId>:<webchat>:direct:<opaque-member-id>
 * A key WITHOUT `:direct:` is a group/channel/cron lane with no single peer
 * identity. Those return empty by construction — a group chat must never
 * dereference to one member's private rep.
 *
 * ── FAIL-CLOSED, UNLIKE THE REST OF THE STACK ────────────────────────────────
 * dinomem-recall-gate fails OPEN because a broken recall nudge must not brick the
 * agent. This tool does the opposite: any parse/read/scope error returns EMPTY,
 * never a partial or unscoped read. The failure mode of a memory tool leaking the
 * wrong person's record is worse than the failure mode of it returning nothing.
 */

// Import path MUST be "openclaw/plugin-sdk/plugin-entry" — the same entry the
// other loaded extensions (dinotrust-enforce, dinomem-recall-gate) use. The
// scoped "@openclaw/plugin-sdk" form resolves in some installs and silently
// fails to load in others; prefer this form.
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import fs from "node:fs";
import path from "node:path";

type Cfg = {
  /** Workspace root holding memory/peers. Empty = derive from ctx.workspaceDir. */
  workspaceDir: string;
  /** Only serve sessionKeys containing this substring; "" = all agents. */
  agentFilter: string;
  /** Max rep characters returned (a rep can grow unbounded over months). */
  maxChars: number;
  /** Max graph edges returned for the scoped peer. */
  maxEdges: number;
  /** Rep sections that are safe to return to the peer themselves. */
  allowSections: string[];
};

function cfg(raw: any): Cfg {
  const r = raw ?? {};
  return {
    workspaceDir: String(r.workspaceDir ?? ""),
    agentFilter: String(r.agentFilter ?? ""),
    maxChars: Number(r.maxChars ?? 4000),
    maxEdges: Number(r.maxEdges ?? 25),
    // LEDGER and PROVENANCE are deliberately EXCLUDED by default: they carry
    // operator-side bookkeeping (how the rep was derived, which archives it came
    // from), which is about our pipeline, not about the person.
    allowSections: Array.isArray(r.allowSections)
      ? r.allowSections.map(String)
      : ["FACTS", "BEAT", "RELATIONS"],
  };
}

/**
 * Derive the peer identity from the RUNTIME-supplied session key.
 * Returns null for any key that does not name exactly one direct peer.
 */
function deriveScope(sessionKey: string): { platform: string; peerId: string } | null {
  if (!sessionKey) return null;
  const parts = sessionKey.split(":");
  // Need at least agent:<id>:<platform>:direct:<peer>
  const d = parts.indexOf("direct");
  if (d < 2 || d + 1 >= parts.length) return null;
  const platform = parts[d - 1];
  // Peer id is everything after "direct:" — rejoined, since some platforms use
  // ':' inside the handle (e.g. whatsapp lids). Never trust a single part.
  const peerId = parts.slice(d + 1).join(":").trim();
  if (!platform || !peerId) return null;
  // Defence in depth: the peer id becomes a FILENAME. Reject anything that could
  // escape the peers directory even though the session key is runtime-supplied.
  if (peerId.includes("/") || peerId.includes("\\") || peerId.includes("..")) return null;
  if (!/^[A-Za-z0-9_.+@-]+$/.test(peerId)) return null;
  if (!/^[A-Za-z0-9_-]+$/.test(platform)) return null;
  return { platform, peerId };
}

/** Extract only the allowlisted sections from a rep file. */
function readRep(repPath: string, allow: string[], maxChars: number): string {
  let txt = "";
  try {
    txt = fs.readFileSync(repPath, "utf8");
  } catch {
    return "";
  }
  const out: string[] = [];
  let current: string | null = null;
  for (const line of txt.split("\n")) {
    const h = /^##\s+(.+?)\s*$/.exec(line);
    if (h) {
      current = h[1].trim().toUpperCase();
      if (allow.includes(current)) out.push(line);
      continue;
    }
    if (current && allow.includes(current)) out.push(line);
  }
  const joined = out.join("\n").trim();
  return joined.length > maxChars ? joined.slice(0, maxChars) + "\n…(truncated)" : joined;
}

/** Scoped graph edges for exactly this peer subject. */
function readEdges(graphPath: string, subject: string, maxEdges: number): string[] {
  try {
    const raw = JSON.parse(fs.readFileSync(graphPath, "utf8"));
    const edges = Array.isArray(raw?.edges) ? raw.edges : [];
    return edges
      .filter((e: any) => e?.subject === subject)
      .slice(0, maxEdges)
      .map((e: any) => `${e.verb} → ${e.object}  (conf ${e.confidence}, last ${e.last_seen})`);
  } catch {
    return [];
  }
}

export default definePluginEntry({
  id: "peer-recall",
  name: "peer recall",
  description:
    "Scoped peer-memory read. Returns ONLY the calling peer's own record; scope is derived server-side from sessionKey and cannot be supplied by the model or the user.",
  register(api: any) {
    const c = cfg(api?.pluginConfig?.config ?? api?.pluginConfig ?? api?.config);

    // Registered as a FACTORY (not a static tool object) precisely so that each
    // tool build receives the live per-session ctx. A static object would have no
    // way to see whose session it is running in.
    api.registerTool((ctx: any) => {
      const sessionKey = String(ctx?.sessionKey ?? "");
      // DIAGNOSTIC 2026-10-12: a MISSING sessionKey at build time must not be
      // treated as "wrong agent". Bailing on "" silently removed the tool
      // entirely. Scope is still derived from sessionKey and execute() still
      // fails CLOSED when scope is null, so relaxing this guard cannot widen
      // what any caller can read.
      if (sessionKey && c.agentFilter && !sessionKey.includes(c.agentFilter)) return null;

      const workspaceDir = c.workspaceDir || String(ctx?.workspaceDir ?? "");
      if (!workspaceDir) return null;

      // Scope is resolved ONCE here, from runtime context only.
      const scope = deriveScope(sessionKey);

      return {
        name: "peer_recall",
        label: "Peer recall",
        description:
          "Recall what you already know about the person you are currently talking to " +
          "(their stated facts, recent beats, and relations). Takes no target: it always " +
          "and only reads the current conversation partner's own record. Returns empty in " +
          "group lanes. Use when the user refers to earlier conversations with you.",
        promptSnippet:
          "peer_recall: recall the current conversation partner's own stored record (no arguments; self-scoped).",
        parameters: {
          type: "object",
          properties: {
            // A FILTER, not a selector. It narrows text already scoped to this
            // peer; it can never widen the scope to another person.
            query: {
              type: "string",
              description:
                "Optional substring filter applied to the already-scoped record (e.g. a topic). Does not and cannot change whose record is read.",
            },
          },
          additionalProperties: false,
        },
        async execute(_toolCallId: string, params: any) {
          try {
            if (!scope) {
              return {
                output:
                  "peer_recall: no single-peer scope for this session (group/non-direct lane). Nothing returned by design.",
                isError: false,
              };
            }

            const peersDir = path.join(workspaceDir, "memory", "peers");
            const repPath = path.join(peersDir, `${scope.platform}_${scope.peerId}.md`);
            // Final containment check: the resolved path must still sit inside
            // the peers dir after normalisation.
            if (!path.resolve(repPath).startsWith(path.resolve(peersDir) + path.sep)) {
              return { output: "peer_recall: scope resolution rejected.", isError: false };
            }

            const subject = `peer:${scope.platform}:${scope.peerId}`;
            const rep = readRep(repPath, c.allowSections, c.maxChars);
            const edges = readEdges(path.join(peersDir, "peers_graph.json"), subject, c.maxEdges);

            if (!rep && edges.length === 0) {
              return {
                output: `peer_recall: no stored record yet for ${subject}.`,
                isError: false,
              };
            }

            let body = rep;
            const q = String(params?.query ?? "").trim().toLowerCase();
            if (q && body) {
              const kept = body
                .split("\n")
                .filter((l) => l.startsWith("## ") || l.toLowerCase().includes(q));
              if (kept.some((l) => !l.startsWith("## "))) body = kept.join("\n");
            }

            const sections = [`# Peer record — ${subject}`];
            if (body) sections.push(body);
            if (edges.length) sections.push(`## GRAPH\n${edges.map((e) => `- ${e}`).join("\n")}`);

            return { output: sections.join("\n\n").trim(), isError: false };
          } catch {
            // Fail CLOSED: never leak a partial or unscoped read.
            return { output: "peer_recall: unavailable.", isError: false };
          }
        },
      };
    }, { names: ["peer_recall"] });
  },
});
